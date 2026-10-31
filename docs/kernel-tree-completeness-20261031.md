# Kernel build tree completeness — 2026-10-31

Question: **does the pinned device tree contain every build input the kernel build actually
references?** R9e recorded one instance of the failure (a defconfig flag whose `= 1` branch runs
a script that exists nowhere). This document generalises it: enumerate every external input
`scripts/build-xnu-4k-baseline.sh` reaches, two levels deep, and say for each whether it is in
the checkout, in the assembled tree, or missing everywhere.

Everything below is cited `file:line`. Where a path's assembly-time existence cannot be
determined from the checkouts alone, the answer is **UNKNOWN**, not a guess.

---

## 0. The answer to the question that matters most

**Can `XNU_NATIVE_VIRTIO_CONSOLE=1` be built? The archive-building script *is* in the pinned
tree — and nothing ever invokes it.**

| | |
|---|---|
| The script that builds the archive | `vendor/UniXNU-qemu/scripts/build-arm-driver.py` |
| It lists the component | `build-arm-driver.py:11` — `NativeVirtioConsole` is choice **8 of 17**, between `NativeMobileDisplay` and `NativeLegacyDisplay` |
| It names the output directory | `build-arm-driver.py:31` — `'NativeVirtioConsole': 'native-virtio-console'` |
| It produces the archive | `build-arm-driver.py:106-108` — `archive = out / ('lib' + component + '.a')`, `xcrun ar rcs` → `build/native-virtio-console/libNativeVirtioConsole.a` |
| The source it compiles | `build-arm-driver.py:28` resolves to exactly one file: `drivers/NativeVirtioConsole/NativeVirtioConsole.cpp`, which **is** in the pinned tree (`vendor/UniXNU/drivers/NativeVirtioConsole/NativeVirtioConsole.cpp`, submodule pinned at `config/sources.lock.json:144-146`) |
| Is that choice ever invoked? | **No.** Every invocation of `build-arm-driver.py` in the repository is listed in §3c; none names `NativeVirtioConsole` |

So this is **not** the R9e failure mode. `scripts/build-boot-progress.py` is missing *everywhere*;
`build-arm-driver.py NativeVirtioConsole` is *present and dormant*. The two need different fixes,
and confusing them would produce the wrong one.

Concretely, with the flag set to 1 and no builder invoked:

- `build-xnu-4k-baseline.sh:192-199` accepts the value (it is a legal `0`/`1`) and adds
  `-DXNU_NATIVE_VIRTIO_CONSOLE=1` to `PERSONALITY_CFLAGS`, which `:200` installs on
  `KernelConfigTables.cpo`. That half works — the IOKit personality block already exists in the
  pinned tree at `vendor/UniXNU/patches/xnu/native-aes-personality.patch:23-29` (note the patch is
  *named* for AES and carries the console personality), gated on exactly that macro.
- `:666-670` then requires the archive:
  ```sh
  if [ "$NATIVE_VIRTIO_CONSOLE" = 1 ]; then
    CONSOLE_ARCHIVE=$ROOT/build/native-virtio-console/libNativeVirtioConsole.a
    test -f "$CONSOLE_ARCHIVE"
  ```
  `test -f` is bare, under `set -euo pipefail` (line 2), with the ERR trap at line 4. The build
  dies naming line 668 and the path, with no explanation of *why* the file is absent.
- The whole archive block is inside `if [ "${XNU_SOURCE_CORECRYPTO14:-0}" = 1 ] && [ "$BOOTSTRAP" = 0 ]`
  at `:545`. `XNU_BOOTSTRAP=1` (set by `scripts/unixnu/kernel.py:90`) skips it, so the **bootstrap
  pass survives** and produces the compile records; the failure lands on the *second*, real pass
  (`kernel.py:102`). That is what makes it expensive rather than merely broken: it is downstream of
  acquisition, the IPSW, the Mesa bootstrap, the GPU addon chain and a full XNU build.

---

## 1. Method, and the one structural fact that changes every "missing" reading

There are **two** pinned checkouts, and the assembled build tree is neither of them:

- `vendor/UniXNU` — the main repo (`config/sources.lock.json` key `unixnu`, fork pin `3919f6f`).
  Holds `drivers/`, `patches/`, `kits/{AdrenoFirmware,AdrenoPM4,AirNir,ArmSmmu4K,NativePrelink,VerifiedRoot}`,
  `scripts/`, `docs/`.
- `vendor/UniXNU-qemu` — the device repo (`config/sources.lock.json` key `device_repo`, fork pin
  `73e5154`; `scripts/unixnu/cli.py:17` is the `-D/--device` flag and `cli.py:90` reads
  `configs/` from it). Holds `scripts/`, `package/`, `board.toml`, `configs/`, `feeds.toml`.

`vendor/UniXNU/scripts/unixnu/tree.py` assembles `out/<device>/tree/`:

- `tree.py:38-41` clears and recreates `drivers/ kits/ patches/ refer/`; `:44-45` creates `build/`
  and `.cache/`.
- `tree.py:62-67` symlinks each resolved module into the tree: `driver` → `drivers/<name>`,
  `kit`/`package` → `kits/<kit>` (with `package`/`main` → `tree/package`), `patch` → `patches/<name>`.
  `<kit>` comes from `board.toml` `[board] kit`; `vendor/UniXNU-qemu/board.toml:11` is
  `kit = "qemu"  # package/ of this repository appears as kits/qemu in the build tree`.
- `tree.py:63-67` **copies** `scripts/` from *both* `ctx.root` and `ctx.device`, skipping
  `__pycache__` and `unixnu/` (`tree.py:29`). Device scripts are copied second, so on a relative-path
  collision the device version wins.

Two consequences that a checkout-only probe gets wrong (this is exactly the R9h trap, and it
applies to the console question too):

1. **`scripts/build-arm-driver.py` is not in `vendor/UniXNU/scripts/` and is not needed there.** It is
   in `vendor/UniXNU-qemu/scripts/` and lands in the assembled tree by the `ctx.device` copy. A probe
   that greps only `vendor/UniXNU/scripts/` reads "missing".
2. **`drivers/NativeVirtioConsole/` is not in `vendor/UniXNU-qemu/`** (that repo has no `drivers/`
   at all — verified: its top level is `LICENSE README.md board.toml configs feeds.toml package
   scripts unixnu.lock`). It is a driver module of `vendor/UniXNU`, symlinked in at `tree.py:67`.
   A probe that greps only the device repo reads "missing".

Both halves exist in the pinned tree and both reach the assembled tree. **The gap is the
invocation, not the file.**

A note on `kits/`: R9h recorded that `kits/` "does not exist in the checkout". In the current
checkout it does — `vendor/UniXNU/kits/` holds six kits. What does *not* exist in any checkout is
`kits/qemu` specifically, because that is the device package and only `tree.py:66` creates it.

---

## 2. Every build input `build-xnu-4k-baseline.sh` references

`vendor/UniXNU/scripts/build-xnu-4k-baseline.sh`, 787 lines. "Checkout" columns name the repo
that holds the file. "Assembly" means it exists in `out/<device>/tree/`.

### 2a. Scripts and shell helpers invoked directly

| Path | Referenced by | In checkout? | At assembly time? | Consequence if missing |
|---|---|---|---|---|
| `scripts/prepare-xnu-source.py` | `build-xnu-4k-baseline.sh:51` | yes — `vendor/UniXNU/scripts/` | yes | n/a (present) |
| `scripts/build-boot-progress.py` | `build-xnu-4k-baseline.sh:70` | **no** | **no** — absent from *both* repos' `scripts/` | build dies at line 70 (R9e). Only reference in the repository is that line |
| `scripts/build-firehose.sh` | `build-xnu-4k-baseline.sh:123` | yes — `vendor/UniXNU/scripts/` | yes | n/a (present) |
| `scripts/seal-source-crypto-kernel.py` | `build-xnu-4k-baseline.sh:785` | yes — `vendor/UniXNU/scripts/` | yes | n/a (present) |
| `scripts/prepare-storage-headers.py` | `scripts/unixnu/kernel.py:99` (conditional on `refer/IOStorageFamily`) | yes — `vendor/UniXNU/scripts/` | yes | n/a (present); the guard at `kernel.py:97` means it only runs when that refer dir exists |

Host tools the script requires without checking: `git` (`:16,:18,:23,:40-47`), `make` (`:30,:115,:774`),
`cmake`/`ninja` (`:32`, via `command -v`), `ditto` (`:93-95`), `unifdef` (`:105`), `python3`
(`:51,:72,:99,:395,:609,:623,:651,:678,:736`), `xcodebuild` and `xcrun` (inside `build-firehose.sh`).
`scripts/unixnu/kernel.py:73-75` checks `git make clang cmake ninja unifdef xcodebuild` up front
with a `brew install` hint, so these fail early and legibly rather than mid-build.

### 2b. Patches (both globs)

| Path | Referenced by | In checkout? | At assembly time? | Consequence if missing |
|---|---|---|---|---|
| `patches/xnu/*.patch` — **53 files** | `build-xnu-4k-baseline.sh:44` | yes — `vendor/UniXNU/patches/xnu/` | yes (`tree.py:67`) | the loop at `:44-49` is the whole patch application; a missing file is a silent glob skip, not an error |
| `patches/xnu/zzz-boot-progress.patch` | `:38` | yes | yes | present |
| `patches/xnu/zzy-staged-system-apps.patch` | `:38` | yes | yes | present |
| `patches/xnu/zzy-native-gpu-addon-mount.patch` | `:38` | yes | yes | present |
| `patches/xnu/native-aes-personality.patch` (carries the `NativeVirtioConsole` personality at `:23-29`) | applied by the `:44` glob; consumed by `:198` + `:200` | yes | yes | if absent, the console flag adds a CFLAG with nothing to compile — a silent no-op rather than a failure |
| `kits/NativePrelink/patches/xnu-10063.141.1-security-init.patch` | `build-xnu-4k-baseline.sh:44` (second glob) | yes — `vendor/UniXNU/kits/NativePrelink/patches/` | yes | present. R9c recorded these two as the only ones that fail to apply on Windows (CRLF checkout artefact), not as missing |
| `kits/NativePrelink/patches/xnu-10063.141.1-sparse-index.patch` | `:44` (second glob) | yes | yes | present, same CRLF note |
| `kits/NativePrelink/emit_prelink.py` | `vendor/UniXNU-qemu/board.toml` `[kernel.prelink] tool` | yes — `vendor/UniXNU/kits/NativePrelink/` | yes | n/a (present) |

### 2c. Fetched or generated at build time — **not** missing

These read as "absent from the checkout" and are not defects. Listed so the table is complete and
so a future probe does not re-report them.

| Path | Referenced by | In checkout? | At assembly time? | Consequence if missing |
|---|---|---|---|---|
| `refer/xnu` | `:15-16` | no | cloned at `:16` at the pinned tag, then commit-checked at `:18-21` | n/a |
| `refer/AvailabilityVersions` | `:26-27` | no | cloned at `:27` | n/a |
| `refer/libplatform` | `:88-89` | no | cloned at `:89` | n/a |
| `refer/libdispatch` | `:96-97` | no | cloned at `:97` | n/a |
| `.cache/xnu-work` | `:22-23` | no | `git clone --shared` at `:23` | n/a |
| `.cache/fakeroot/usr/local/libexec/availability.pl` | `:25` | no | built by `make -C refer/AvailabilityVersions install` at `:30-32` | n/a |
| `.cache/fakeroot/usr/local/lib/kernel/libfirehose_kernel.a` | `:122` | no | built by `scripts/build-firehose.sh` at `:123` | n/a |
| `.cache/libdispatch-work` | inside `scripts/build-firehose.sh` | no | `git clone --shared refer/libdispatch` there | n/a |
| `build/xnu/ios-obj/DEVELOPMENT_ARM64_BCM2837/iokit/DEVELOPMENT/IOPlatformExpert.cpo.json` | `vendor/UniXNU-qemu/scripts/build-arm-driver.py:26-27` | no | written by the bootstrap pass (`scripts/unixnu/kernel.py:83-95`) | the driver builders cannot run without it; `kernel.py:84-95` exists precisely to produce it |
| `build/xnu/ios-obj/EXPORT_HDRS/libsa` | `vendor/UniXNU-qemu/scripts/build-os-root.py:85-87` | no | created by the kernel build, re-created defensively there | n/a |

### 2d. Archives the kernel link requires, and who builds them

Every row below is a bare `test -f` inside the block at `:545-735`, gated on
`XNU_SOURCE_CORECRYPTO14=1 && BOOTSTRAP=0`. All are build outputs, so "in checkout" is *no* for
all of them by nature; the column that matters is **who builds it**, and that is where the
console row differs.

| Archive | `test -f` at | Builder | Builder in pinned tree? | Builder invoked? |
|---|---|---|---|---|
| `build/corecrypto-2023/libcorecrypto_kernel_noasm-namespaced.a` | `:546-547` | `vendor/UniXNU/scripts/build-corecrypto-14.py` | yes | yes — `vendor/UniXNU-qemu/board.toml:32-33` |
| `build/armdt-platform/libArmDTPlatform.a` | `:581-582` | `build-arm-driver.py ArmDTPlatform` | yes (`vendor/UniXNU-qemu/scripts/`) | yes — `board.toml:34` |
| `build/armcpu/libArmCpu.a` | `:587-588` | `build-arm-driver.py ArmCpu` | yes | yes — `board.toml:35` |
| `build/native-aes/libNativeAES.a` | `:637-638` | `build-arm-driver.py NativeAES` | yes | yes — `board.toml:36` |
| `build/native-ufs/libNativeUfs.a` | `:593-594` | `build-arm-driver.py NativeUfs` | yes (choice 14/17) | **no** — dormant, same class as the console |
| `build/native-adreno/libNativeAdreno.a` | `:601-602` | `build-arm-driver.py NativeAdreno` | yes (choice 17/17) | **no** — dormant |
| `build/native-mobile-display/libNativeMobileDisplay.late.a` | `:607-608` | `build-native-legacy-display.py` | yes | yes — `build-os-root.py:138` |
| `build/native-legacy-display/libNativeLegacyDisplay.late.a` | `:621-622` | `build-arm-driver.py NativeLegacyDisplay` | yes | yes — `build-native-legacy-display.py:100` |
| `build/native-virtio-block/libNativeVirtioBlock.a` | `:643-644` | `build-arm-driver.py NativeVirtioBlock` | yes | yes — `build-os-root.py:89` |
| `build/native-virtio-block-verified-bundle/libNativeVirtioBlock.a` | `:643-644` (via `XNU_NATIVE_VIRTIO_BLOCK_ARCHIVE`, `qemu_root_defconfig`) | `build-arm-driver.py NativeVirtioBlock --verified-root --verified-root-bundle` | yes | yes — `build-os-root.py:89` |
| `build/verified-root-os-xnu-bundle/libVerifiedRootProbe.a` | `:710-711` (via `XNU_VERIFIED_ROOT_ARCHIVE`) | `build-verified-root-probe.py --component os --include-systemos` | yes | yes — `build-os-root.py:88` |
| `build/native-ram-block/libNativeRamBlock.a` | `:673-674` | `build-arm-driver.py NativeRamBlock` | yes | yes — `build-os-root.py:93` |
| `build/libpthread-kernel/libpthread_kernel.a` + `source_pthread_bootstrap.o` | `:702-705` | `vendor/UniXNU/scripts/build-libpthread-kernel.py` | yes | yes — `board.toml:38` |
| `build/mockfs-fixture/libmockfs_fixture.a` | `:716-717` | `vendor/UniXNU/scripts/build-mockfs-fixture.py` | yes | yes — `board.toml:39` |
| `build/sheng-platform-power/libShengPlatformPower.a` | `:722-723` | `vendor/UniXNU-qemu/scripts/build-platform-power.py` | yes | yes — `board.toml:41` |
| `build/link-stubs/libLinkStubs.a` | via `CONFIG_XNU_EXTRA_LD_ARCHIVES`; checked at `scripts/unixnu/kernel.py:66-68` | `vendor/UniXNU-qemu/scripts/build-link-stubs.py` | yes | yes — `board.toml:40` |
| `build/native-offline-gpu/libNativeOfflineGPU.late.a` | `:649-650` | `build-arm-driver.py NativeOfflineGPU` | yes (choice 11/17) | **no** — dormant |
| `build/boot-progress/libBootProgress.a` | `:729` | `scripts/build-boot-progress.py` | **no** | **no — missing everywhere** (R9e) |
| **`build/native-virtio-console/libNativeVirtioConsole.a`** | **`:667-668`** | **`build-arm-driver.py NativeVirtioConsole`** | **yes** | **no — present but never invoked** |

`build/native-offline-gpu/libNativeOfflineGPU.late.a` at `:649-650` carries a *second* reference
to the console archive: the inline Python at `:651-662` hashes
`build/native-virtio-console/libNativeVirtioConsole.a` and compares it to
`report['console_archive_sha256']` (`:660-661`). So the offline GPU broker both requires the
console flag (`:311-313`: "The private offline GPU broker requires the actual byte transport")
and requires the console archive to be byte-identical to the one it was built against. Neither
`CONFIG_XNU_NATIVE_OFFLINE_GPU` nor `CONFIG_XNU_NATIVE_VIRTIO_CONSOLE` appears in
`vendor/UniXNU-qemu/configs/qemu_defconfig` or `qemu_root_defconfig` (verified by grep), so this
is a latent chain, not a live one.

### 2e. Paths that exist only at assembly time

These are the R9h class: absent from every checkout under the name the caller uses, present in
the assembled tree. A checkout-only probe reports them as missing.

| Path the caller uses | Referenced by | In checkout under that name? | At assembly time? | Consequence if missing |
|---|---|---|---|---|
| `kits/qemu/LinkStubs.c` | `vendor/UniXNU-qemu/scripts/build-link-stubs.py:7` | no — present as `vendor/UniXNU-qemu/package/LinkStubs.c` | **yes**, `tree.py:66` symlinks `package/` → `kits/qemu` | the link-stubs archive the kernel link needs (`kernel.py:66-68`) cannot be built |
| `kits/qemu/LegacyDisplayProbe.cpp` | `build-native-legacy-display.py:180` | no — `vendor/UniXNU-qemu/package/LegacyDisplayProbe.cpp` (40,224 bytes) | yes | the legacy display archive fails |
| `kits/qemu/KeyStoreProbe.cpp` | `build-native-legacy-display.py:191` | no — `package/KeyStoreProbe.cpp` | yes | as above |
| `kits/qemu/HidInputProbe.cpp` | `build-native-legacy-display.py:195` | no — `package/HidInputProbe.cpp` | yes | as above |
| `kits/qemu/TouchState.cpp` | `build-native-legacy-display.py:198` | no — `package/TouchState.cpp` | yes | as above |
| `kits/qemu/VirtioInput.cpp` | `build-native-legacy-display.py:201` | no — `package/VirtioInput.cpp` | yes | as above |
| `kits/qemu/SharedWindow.cpp` | `build-native-legacy-display.py:204` | no — `package/SharedWindow.cpp` | yes | as above |
| `kits/qemu/QemuMetal/QemuMetalPublish.cpp` | `build-native-legacy-display.py:210` | no — `package/QemuMetal/QemuMetalPublish.cpp` | yes | the QemuMetal publish half fails |
| `kits/qemu/QemuMetal/QemuMetalAccelerator.cpp` | `build-native-legacy-display.py:213` | no — `package/QemuMetal/QemuMetalAccelerator.cpp` | yes | the accelerator half fails |
| `kits/qemu/QemuMetal` (directory, 10 files) | `vendor/UniXNU-qemu/scripts/build-qemu-metal.py:37` | no — `package/QemuMetal/` | yes | the QemuMetal bundle cannot be built |
| `kits/qemu` (as `PACKAGES_PATH`) | `vendor/UniXNU-qemu/scripts/build-qemu-ramfb-efi.py:31-33` | no | yes | the ramfb EFI build cannot find its package |
| `kits/VerifiedRoot/include` | `build-arm-driver.py:72`, `:78` | **yes** — `vendor/UniXNU/kits/VerifiedRoot/` | yes | n/a (present) |
| `kits/ArmSmmu4K`, `kits/AdrenoPM4`, `kits/AdrenoFirmware` | `build-arm-driver.py:85-87` | **yes** — `vendor/UniXNU/kits/` | yes | n/a (present) |
| `kits/sheng` | `build-arm-driver.py:86` (`-I` for `NativeAdreno`) | **no** — `vendor/UniXNU/kits/` holds exactly six kits (`AdrenoFirmware AdrenoPM4 AirNir ArmSmmu4K NativePrelink VerifiedRoot`), and `vendor/UniXNU-qemu/` has no `kits/` at all | **no** — it is a kit of the *sheng* device repository, which is not this board's `ctx.device`, so `tree.py:66` never creates it | `NativeAdreno` cannot compile. Moot today because nothing invokes `NativeAdreno` (§2d) — a fourth instance of the same present-but-dormant class |
| `build/sheng-gmu` | `build-arm-driver.py:87` (`-I` for `NativeAdreno`) | **no** (a build output) | **no** — no script in either pinned tree writes it; the only reference anywhere is that `-I` line | same as `kits/sheng`: dormant, and a build output with no producer is the same shape as a script with no invoker |
| `drivers/NativeVirtioConsole/NativeVirtioConsole.cpp` | `build-arm-driver.py:28` | **yes** — `vendor/UniXNU/drivers/NativeVirtioConsole/` | yes, `tree.py:67` | n/a (present) |
| `drivers/NativeVirtioConsole` (as an include dir) | `build-arm-driver.py:81`, consumed by `vendor/UniXNU/drivers/NativeMetal/NativeOfflineGPU.cpp:8` | yes | yes | `NativeOfflineGPU` cannot find `NativeVirtioConsoleTransport.h` |
| `build/storage-abi/include` | `build-arm-driver.py:70` | no | built by `vendor/UniXNU-qemu/scripts/build-storage-abi.py` (`build-os-root.py` first step) | the storage drivers fail to compile |

---

## 3. What the console driver needs, and whether it can be built

### 3a. The device it probes for

`vendor/UniXNU/drivers/NativeVirtioConsole/NativeVirtioConsole.cpp`, `probe()` at `:94-106`:

| Requirement | Value | file:line |
|---|---|---|
| DT property read | `provider->getProperty("compatible")` | `NativeVirtioConsole.cpp:95` |
| `compatible` string | exactly `"virtio,mmio"`, length 12 | `:96` |
| MMIO window | `mapDeviceMemoryWithIndex(0)`, length ≥ `0x100` | `:97`, `:100` |
| Magic value (VirtIO MMIO `MagicValue`) | `0x74726976` (`76 69 72 74` = `"virt"`) at offset 0 | `:101` |
| Version | `2` (VirtIO 1.x modern MMIO) at offset 4 | `:102` |
| Device id | `3` (VirtIO console) at offset 8 | `:103` |
| Re-validated in `start()` | the same three words, `read32(0)/read32(4)/read32(8)` | `:209` |
| Scope | port 0 only, no MULTIPORT / event-index / indirect descriptors | `:2`, `:134-135` |
| Queue geometry | `queueSize=8`, `bufferSize=4096` | `:13` |

The kernel-side match also needs the personality block, which is present and correctly gated:
`vendor/UniXNU/patches/xnu/native-aes-personality.patch:23-29`:
```
+#if defined(XNU_NATIVE_VIRTIO_CONSOLE) && XNU_NATIVE_VIRTIO_CONSOLE
+    "   ,{"
+    "     'IOClass'         = NativeVirtioConsole;"
+    "     'IOProviderClass' = IOPlatformDevice;"
+    "     'IOProbeScore'    = 1000:32;"
+    "   }"
+#endif
```

### 3b. Is it in `build-arm-driver.py`'s choices list?

**Yes.** `vendor/UniXNU-qemu/scripts/build-arm-driver.py:11`:
```python
parser.add_argument('component', choices=['ArmVirtPlatform', 'ArmDTPlatform', 'ArmCpu', 'NativeAES', 'NativeVirtioBlock', 'NativeRamBlock', 'NativeMobileDisplay', 'NativeVirtioConsole', 'NativeLegacyDisplay', 'NativeGOPPresenter', 'NativeOfflineGPU', 'NativeOfflineAccelerator', 'NativeIOGPUProviderProbe', 'NativeUfs', 'NativeHidInput', 'NativeKeyStoreEvents', 'NativeAdreno'])
```
`NativeVirtioConsole` is the 8th of 17. Its invocation form is the single positional argument,
`python3 scripts/build-arm-driver.py NativeVirtioConsole` — none of the optional flags apply
(`:17-24` rejects them for other components).

### 3c. Does anything in the pinned tree invoke that choice?

**No.** Every invocation of `build-arm-driver.py` in the repository (excluding `local-private/`):

| Invoker | Components |
|---|---|
| `vendor/UniXNU-qemu/board.toml:34` | `ArmDTPlatform` |
| `vendor/UniXNU-qemu/board.toml:35` | `ArmCpu` |
| `vendor/UniXNU-qemu/board.toml:36` | `NativeAES` |
| `vendor/UniXNU-qemu/scripts/build-os-root.py:89` | `NativeVirtioBlock --verified-root --verified-root-bundle` |
| `vendor/UniXNU-qemu/scripts/build-os-root.py:93` | `NativeRamBlock` |
| `vendor/UniXNU-qemu/scripts/build-native-legacy-display.py:100` | `NativeLegacyDisplay` |
| `vendor/UniXNU-qemu/scripts/build-native-legacy-display.py:247` | `NativeKeyStoreEvents` |
| `vendor/UniXNU-qemu/scripts/build-native-legacy-display.py:262` | `NativeGOPPresenter` |
| `vendor/UniXNU/scripts/build-native-hid-input.py:77` | `NativeHidInput` (itself dormant — nothing invokes `build-native-hid-input.py`) |

`vendor/UniXNU-qemu/board.toml:31-42` is the `[kernel] archives` list; it has no
`NativeVirtioConsole` entry. `vendor/UniXNU-qemu/configs/qemu_defconfig` and
`qemu_root_defconfig` have no `CONFIG_XNU_NATIVE_VIRTIO_CONSOLE` line.

This is not an oversight anyone hid. `src/unipad/host/unixnu_boot.py:1465-1471` states it:
> ``NativeVirtioConsole`` exists in the device repository but is not in this board's build --
> ``build-arm-driver.py``'s choices list is the only thing that mentions it and nothing invokes
> it -- so the guest kernel this host runs will enumerate nothing on this transport, and the
> port will accept connections and stay silent.

and the same claim is asserted as a test contract at `tests/unit/test_host_unixnu_boot.py:1066`.

### 3d. So: buildable?

**Buildable in principle, unbuilt in practice, and unverified end to end.**

What is present and sufficient:
- the builder, `vendor/UniXNU-qemu/scripts/build-arm-driver.py:11,31,106-108`;
- the source, `vendor/UniXNU/drivers/NativeVirtioConsole/NativeVirtioConsole.cpp` (its only
  include beyond the SDK is `"NativeVirtioConsoleTransport.h"` at `:11`, which is in the same
  directory, so the quoted include resolves without any `-I`);
- the compile record it reads, `IOPlatformExpert.cpo.json`, which the bootstrap pass exists to
  produce (`scripts/unixnu/kernel.py:83-95`);
- the personality patch, `native-aes-personality.patch:23-29`;
- the flag validation and the archive link line, `build-xnu-4k-baseline.sh:192-199` and `:666-670`.

What is absent:
- any invocation of the choice (§3c);
- a QEMU console device. `vendor/UniXNU-qemu/scripts/run-qemu.py:210` attaches
  `virtio-tablet-device` and `virtio-keyboard-device`, `:203`/`:207` attach the two
  `virtio-blk-device`s, and `:199` sets `-global virtio-mmio.force-legacy=false` (which is what
  the driver's version-2 requirement needs). There is **no** `virtio-console-device`. So even
  linked, `probe()` would find no `virtio,mmio` provider and return `nullptr` at `:96`.
- a measurement that `NativeVirtioConsole.cpp` compiles clean under
  `build-arm-driver.py:66-68`'s `-Wall -Wextra -Werror` with `-Wno-unused-parameter`. **UNKNOWN** —
  no toolchain on this host. Recorded as UNKNOWN, not as pass.

---

## 4. Does `build-xnu-4k-baseline.sh` validate its own inputs?

**For flag *values*, yes, exhaustively and legibly.** Every `XNU_*` switch has the same shape —
read with a default, reject anything outside the legal set with a named message and `exit 1`:

| Lines | Variable | Rejects |
|---|---|---|
| `:55-58` | `XNU_BOOTSTRAP` | anything but `0`/`1` |
| `:60-63` | `XNU_BOOT_PROGRESS` | anything but `0`/`1` |
| `:128-131` | `XNU_ARM_DT_PLATFORM` | anything but `0`/`1` |
| `:142-145` | `XNU_CRYPTO_BOOTSTRAP` | anything but `source`/`prelinked` |
| `:178-181` | `XNU_NATIVE_RAM_BLOCK` | anything but `0`/`1` |
| `:193-196` | `XNU_NATIVE_VIRTIO_CONSOLE` | anything but `0`/`1` |
| `:202-205`, `:214-217`, `:231-234`, `:244-247`, `:253-256`, `:262-265`, `:271-274`, `:280-283`, `:289-292`, `:298-301`, `:307-310`, `:316-319`, `:331-334`, `:336-339`, `:352-355`, `:361-364`, `:370-373`, `:387-390`, `:425-428`, `:440-443`, `:453-456`, `:474-478`, `:491-494`, `:502-505`, `:513-516`, `:528-531` | the rest of the switches | anything but `0`/`1` |

It also validates *combinations*, which is the more valuable half — `:65-68` (boot progress
requires UFS/owner-root/Virtio/source-corecrypto), `:132-136`, `:137-140`, `:162-165`, `:171-174`,
`:183-186`, `:218-221`, `:248-251`, `:257-260`, `:266-269`, `:275-278`, `:284-287`, `:293-296`,
`:302-305`, `:311-314`, `:320-323`, `:356-359`, `:365-368`, `:374-377`, `:390-393`, `:429-432`,
`:443-446`, `:457-460`, `:479-483`, `:517-520`.

**For *input existence*, no — with one exception.** Every archive is a bare
`test -f "$ARCHIVE"` (`:547`, `:552`, `:558`, `:564`, `:570`, `:576`, `:582`, `:588`, `:594`,
`:602`, `:608`, `:622`, `:638`, `:644`, `:650`, `:668`, `:674`, `:694`, `:704`, `:705`, `:711`,
`:717`, `:723` — 24 sites, verified by grep). This is deliberate, and the script says so in
its own header comment at `:3`: *"Many checks are bare `test -f`; name the one that stops the
build."* Under `set -e` and the trap at `:4` these produce
`build-xnu-4k-baseline.sh: line 668: test -f .../libNativeVirtioConsole.a failed` — the path, but
not the reason. Nothing cross-checks "is there a builder for this archive in the tree".

The one place that does check is upstream of the shell: `scripts/unixnu/kernel.py:60-62` verifies
each `[kernel] archives` entry is a file before running it, and `:66-68` refuses an
`*_ARCHIVE` config value that no builder makes. But `kernel.py:54` only runs that whole function
when the resolved config carries `XNU_BUILD_ARCHIVES`, and neither `qemu_defconfig` nor
`qemu_root_defconfig` declares it — `scripts/macos/build-macos.sh:2705-2714` appends
`CONFIG_XNU_BUILD_ARCHIVES=y` to the generated `.config` and records the divergence deliberately,
because "It asks for archives it never builds". So the guard is real but lives in this project's
host script, not in the pinned tree, and it covers the `board.toml` list rather than the
`build-xnu-4k-baseline.sh` `test -f` set.

**What happens on this host's build path if the flag is set to 1 and the archive script is absent:**
`XNU_NATIVE_VIRTIO_CONSOLE=1` is accepted at `:193-196`; the CFLAG is installed at `:198`/`:200`;
the bootstrap pass skips the archive block at `:545` and succeeds; `scripts/unixnu/kernel.py:101`
runs the archive builders; `:102` runs the real pass, which reaches `:666-670` and dies at the
bare `test -f` on `:668`. On this Windows host the build cannot run at all
(`scripts/unixnu/kernel.py:75` requires `xcodebuild`, and `build-firehose.sh` calls `xcodebuild`
and `xcrun lipo`), so the observable consequence here is the *shape* of the failure, recorded
from the script, not a reproduction. The runner-side shape is the same one R9e measured for
`build-boot-progress.py`, one block later in the file.

---

## 5. The diff that would enable the console (NOT APPLIED)

Prerequisite, restated: `build-arm-driver.py NativeVirtioConsole` and the driver source are both
already in the pinned tree and both reach the assembled tree. The only missing piece is the
invocation. `scripts/unixnu/kernel.py:101-102` runs `build_archives()` **before** the real pass,
and `build_archives()` runs every `[kernel] archives` entry of `board.toml` (`kernel.py:56-64`),
and `build-macos.sh:2705-2714` injects `CONFIG_XNU_BUILD_ARCHIVES=y` for **both** defconfigs. So
one line in `board.toml` is sufficient to build the archive before the link needs it.

Two files, both in the pinned device repo `vendor/UniXNU-qemu` — which means a fork commit and a
`config/sources.lock.json` re-pin, exactly as R9k/R9l did for the QemuMetal hand-off:

```diff
--- a/vendor/UniXNU-qemu/board.toml
+++ b/vendor/UniXNU-qemu/board.toml
@@ -31,12 +31,13 @@
 archives = [
   "build-corecrypto-14.py",
   "build-corecrypto-14.py --namespace",
   "build-arm-driver.py ArmDTPlatform",
   "build-arm-driver.py ArmCpu",
   "build-arm-driver.py NativeAES",
+  "build-arm-driver.py NativeVirtioConsole",
   "build-native-aes-bindings.py",
   "build-libpthread-kernel.py",
   "build-mockfs-fixture.py",
   "build-link-stubs.py",
   "build-platform-power.py",
 ]
```
(`vendor/UniXNU-qemu/board.toml:31-42`; the new entry goes after `NativeAES` at `:36` and before
`build-native-aes-bindings.py` at `:37`, matching the order the link consumes them.)

```diff
--- a/vendor/UniXNU-qemu/configs/qemu_root_defconfig
+++ b/vendor/UniXNU-qemu/configs/qemu_root_defconfig
@@ -39,6 +39,7 @@
 CONFIG_XNU_OWNER_DATA_MOUNT=y
 CONFIG_XNU_NATIVE_LEGACY_DISPLAY=y
+CONFIG_XNU_NATIVE_VIRTIO_CONSOLE=y

 CONFIG_XNU_CRYPTO_BOOTSTRAP="prelinked"
```
(`vendor/UniXNU-qemu/configs/qemu_root_defconfig`, after `CONFIG_XNU_NATIVE_LEGACY_DISPLAY=y`,
before the blank line at `:40`.)

**Files and lines the diff touches:** `vendor/UniXNU-qemu/board.toml` (one added line in the
`[kernel] archives` list, `:29-42`) and `vendor/UniXNU-qemu/configs/qemu_root_defconfig` (one
added line after `CONFIG_XNU_NATIVE_LEGACY_DISPLAY=y`). Nothing under `vendor/UniXNU/`,
`src/`, `tests/` or `.github/` needs to change for the *build*.

**What the diff does not do, and must not be read as doing:**

1. It does not give the guest a console device. `vendor/UniXNU-qemu/scripts/run-qemu.py:210`
   would need a `virtio-console-device` (and the generated device tree would need a `virtio,mmio`
   node), or `probe()` returns `nullptr` at `NativeVirtioConsole.cpp:96`. **UNKNOWN** whether the
   existing `--virtio-mmio-fdt` path (`vendor/UniXNU-qemu/scripts/make-qemu-adt.py:103`) publishes
   such a node; it was never needed, because no virtio console device was ever attached.
2. It does not prove the source compiles under `-Werror` (`build-arm-driver.py:66-68`).
   **UNKNOWN** without a toolchain.
3. It moves `kernel.macho`'s bytes, because `:669` force-loads a new archive into the link.
   R9l recorded the second-pass kernel at 125,431,808 bytes / sha256 `c62d39ab…`; that value will
   change and any consumer that pins the *built* kernel's digest (as opposed to the IPSW
   `kernelcache_sha256` at `board.toml [kernel.prelink]`) has to be re-pinned. The trust-cache
   allowlist at `board.toml [payload] trust_cache_sha256` is not affected — it pins the QemuMetal
   bundle, not the kernel — but this is the exact class of change R9k recorded as "a visible
   re-pin, not a silent one".
4. It is a change to a *pinned fork*, so it needs the R9k/R9l dance: fork commit, read the SHA
   back, re-pin `config/sources.lock.json` `device_repo.commit`, and note that
   `tests/unit/test_p0p1_gate_main.py` checks the second clone at
   `local-private/upstream/UniXNU-qemu` as well.

---

---

## 6. How a runtime-added virtio-mmio device becomes visible to an ADT guest

This answers the UNKNOWN §5 point 1 left open. The short version: **the ADT carries
32 *generic* `virtio,mmio` placeholder windows, QEMU's device list consumes slots in
that fixed set rather than adding to it, and a `virtio-serial-device` therefore needs
no ADT change at all.** Measured, not inferred, on the host's own QEMU 11.1.0.

### 6a. Q1 — the ADT is the tree the guest parses; QEMU's DTB is not

The chain, end to end:

| Step | Where |
|---|---|
| `board.toml` names the ADT tool | `vendor/UniXNU-qemu/board.toml` `[payload.adt]` → `tool = "scripts/make-qemu-adt.py"` |
| the ADT is built from a **bare-machine** DTB dump | `vendor/UniXNU-qemu/scripts/make-qemu-adt.py:100-103` runs `qemu-system-aarch64 -M virt,... -dumpdtb=<fdt> ... -nodefaults` with **no devices**, then passes that DTB to UniXNU's `scripts/qemu-entry/make-dt.py --virtio-mmio-fdt` |
| the DTB's virtio nodes are copied into the ADT | `vendor/UniXNU/scripts/qemu-entry/make-dt.py:95-138` |
| the ADT is written to the build tree | `make-qemu-adt.py:61` (`--output`, default `build/qemu/boot/kernel.macho.adt`) |
| the ADT is staged onto the ESP | `vendor/UniXNU-qemu/scripts/run-qemu.py:112` → `EFI/UniXNU/Payloads/kernel.macho.adt` |
| the ADT is edited in place just before boot | `run-qemu.py:142-147` (memory-map entries), then rewritten |
| **DarwinBoot loads the ADT and hands it to the kernel** | `vendor/UniXNU/drivers/DarwinBoot/DarwinBootPkg/Driver/DarwinBoot.c:547-550` derives `TreePath` by appending `L".adt"` to `Request->KernelPath` and reads it; `:557` validates it with `AdtNode` (it must consume exactly `TreeSize` or the load fails `EFI_COMPROMISED_DATA`); `:633` copies it into the boot window; **`:630-631` sets `Args->DeviceTree` and `Args->DeviceTreeLength`** |

Two negative measurements that close the "or does the guest also parse QEMU's DTB"
question:

- `run-qemu.py`'s whole `command` list (`:154-212`) contains **no `-dtb` flag** — verified
  by reading the construction in full. QEMU still generates its DTB and places it in
  memory, but nothing hands it to the kernel.
- the guest side reads no `fw_cfg`: there is no `fw_cfg` reference anywhere in
  `vendor/UniXNU/drivers/DarwinBoot/` or `vendor/UniXNU/scripts/qemu-entry/`. The
  `fw_cfg` uses that exist (`run-qemu.py:166`, `:178`, `:182`) are host→guest *data*
  channels (ramfb size, the shared window, the metal clients), not a device tree.

So: **the ADT is the device tree the guest kernel parses, byte for byte.**

### 6b. Q2 — 32 *generic* placeholders, counted twice

**In a delivered ADT** (read-only, binary parsed with the format `make-qemu-adt.py:21-33`
documents — `<uint32 propertyCount><uint32 childCount>`, then per property a 32-byte
key, a `<uint32 length>` and 4-padded data, children recursive):

| ADT | bytes | parse | `virtio,mmio` nodes | `reg` sizes | bases |
|---|---|---|---|---|---|
| `local-private/tmp/bundle10/kernel.macho.adt` | 17,820 | ends exactly at EOF | **32** | 512 × 32 | 0x2000000 … 0x2003e00 |
| `local-private/tmp/bundle7/kernel.macho.adt` | 17,768 | ends exactly at EOF | **32** | 512 × 32 | 0x2000000 … 0x2003e00 |

- The nodes are named `virtio-mmio-0` … `virtio-mmio-31`.
- **Every one carries exactly the same four keys**: `compatible`, `dma-coherent`,
  `name`, `reg`. Nothing distinguishes one from another.
- **No node names a specific virtio device type** — there is no `virtio-serial`,
  `virtio-console`, `virtio-blk` or similar anywhere in either tree. They are
  placeholders, not a device list.

**In QEMU itself** (the host's own `local-private/tools/qemu/qemu-system-aarch64.exe`,
`VERSION` = **11.1.0**, the version the project pins): dumping the DTB of the bare
`virt` machine — the same operation `make-qemu-adt.py:100-103` performs — gives
**32** `virtio,mmio` nodes at bases **0x0a000000 … 0x0a003e00**, each **0x200** wide.

The two sets are the same set: `make-dt.py:138` writes `reg = (address - 0x08000000,
size)`, so ADT base 0x2000000 is physical 0x0a000000, and `make-dt.py:174`'s `arm-io`
`ranges` maps child 0 → parent 0x08000000. Measured ADT bases 0x2000000…0x2003e00 ↔
measured QEMU bases 0x0a000000…0x0a003e00.

### 6c. The decisive measurement — attaching a device *consumes* a slot, it does not *add* one

Four DTB dumps from the same binary, same machine, same memory, differing only in
`-device` arguments:

| DTB dump | extra devices | `virtio,mmio` nodes | sizes | base set |
|---|---|---|---|---|
| bare | — | 32 | 512 × 32 | 0x0a000000…0x0a003e00 |
| two-blk | 2 × `virtio-blk-device` | 32 | 512 × 32 | identical |
| four-like-runqemu | 2 × blk + tablet + keyboard | 32 | 512 × 32 | identical |
| plus-serial | the four + `virtio-serial-device` | 32 | 512 × 32 | identical |

**Attaching a device changes nothing in the DTB node set** — not the count, not the
addresses, not the sizes. The 32 windows are QEMU's fixed transport region for the
`virt` machine; a device is placed *into* one of them.

Instrument validated before trusting that last row: `virtio-serial-device` is accepted
(exit 0, empty stderr) while `virtio-bogus-xyz-device` is refused (exit 1,
`'virtio-bogus-xyz-device' is not a valid device model name`). So the +5 case really
did attach the device rather than silently ignoring it.

**Consequence: the pre-built ADT already describes the window a console device lands
in. No ADT regeneration, no `make-qemu-adt.py` change, no `board.toml [payload.adt]`
change.**

### 6d. Q3 — a free slot exists, and what `probe()` needs is already in the node

**Free slots.** `run-qemu.py:154-212` consumes **4** MMIO slots when `--os-root` is on:
two `virtio-blk-device` (`:203`, `:207`) and `virtio-tablet-device` +
`virtio-keyboard-device` (`:210`). The ESP is `virtio-blk-pci` (`:158`) and consumes
none, and `ramfb` / `qemu-xhci` / `usb-kbd` are not virtio-mmio. So **28 of 32 free**
on the runner path.

The host's `build_argv` consumes up to **5**: `virtio-blk-device,drive=osroot`
(`src/unipad/host/unixnu_boot.py:1862`), `virtio-blk-device,drive=systemos` (`:1774`),
tablet + keyboard (`:1891-1892`), and — **already present today** —
`virtio-serial-device,id=unipad-agent-serial` (`:1496`). So **27 of 32 free**, and the
console device is *already attached* whenever the agent channel is requested.

**What `probe()` needs, and where each requirement is met:**

| Requirement | `NativeVirtioConsole.cpp` | Met by |
|---|---|---|
| `compatible` exactly `"virtio,mmio"`, length 12 | `:95-96` | every one of the 32 ADT nodes (measured: all four keys identical, `compatible` = `virtio,mmio`) |
| `mapDeviceMemoryWithIndex(0)` length ≥ `0x100` | `:100` | the window is **0x200** (measured) |
| magic `0x74726976` | `:101` | QEMU's VirtIO MMIO `MagicValue`, re-read at `:209` |
| version `2` | `:102` | requires `force-legacy=false` — see 6e |
| device id `3` | `:103` | the VirtIO console id; see the caveat below |

**The matching mechanism is the same one already measured working.**
`patches/xnu/native-aes-personality.patch:23-29` installs the console personality as
`IOProviderClass = IOPlatformDevice`, `IOProbeScore = 1000:32` — byte-for-byte the
same shape as `NativeVirtioBlock`'s at `:16-21` of the same patch. So the console
driver probes every `IOPlatformDevice` nub exactly as the block driver does, and the
block driver is already measured to bind a runtime-attached `virtio-blk-device`
(`_native_virtio_verified_devices` 1→2, and `native_virtio_input_devices = 2` for the
tablet + keyboard).

**The precedent for the device pair is upstream's own, and it is the one that needs
this transport.** `vendor/UniXNU/scripts/qemu-entry/gdb-stop.py:373-375` — the offline
GPU broker, which `build-xnu-4k-baseline.sh:311-313` gates on "the actual byte
transport" — emits exactly `-global virtio-mmio.force-legacy=false`, `-chardev socket`,
`-device virtio-serial-device,id=offline-serial` and `-device virtconsole,chardev=
offline-console,name=offline-transport`. `unixnu_boot.py:1455-1462` calls that shape
"measured working", and `_agent_argv` (`:1494-1497`) reproduces it with a TCP chardev
because Windows has no `AF_UNIX`.

**Caveat, stated rather than rounded up.** The one value I could not measure on this
host is `read32(0x08) == 3`. Reading a guest MMIO register needs a full boot, and
`qemu-system-aarch64` is not on this host's default PATH (it lives under
`local-private/tools/qemu/`, which I used for the DTB measurements only). What I can
say precisely: `NativeVirtioConsole.cpp:103` requires device id 3, and the driver's own
header comment (`:2`) names it "Modern VirtIO console port 0. OASIS VirtIO 1.2 sections
4.2 and 5.3". **Verified by precedent, not measured here**: `NativeVirtioBlock.cpp:506`
requires `read32(0x08) == 2` on the same transport and the same register map, and a
`virtio-blk-device` bound. Different device id, identical everything else.

### 6e. Q4 — the window is 0x200, and `force-legacy=false` is required for this driver too

**Width: 0x200 (512), not 0x100.** Measured in both artifacts — all 32 ADT `reg` sizes
are 512, and all 32 QEMU DTB `reg` sizes are 512. Three consequences:

- `NativeVirtioConsole.cpp:100` requires ≥ `0x100` → **satisfied**.
- `NativeVirtioBlock.cpp:499-506` requires ≥ `0x108` → also satisfied, which is why
  the same window serves the block driver.
- `make-dt.py:119` *rejects* a window with `size < 0x108`, so a 0x100 mapping would
  break ADT generation outright. The 0x200 is load-bearing, not incidental.

**`-global virtio-mmio.force-legacy=false` is required, for the same reason as the
block driver.** `docs/boot-windows.md:392-397` records that without the global the MMIO
transport stays in legacy mode and the register offsets differ from what the driver
expects, naming `NativeVirtioBlock.cpp:499-506`'s `read32(0x00)==0x74726976`,
`read32(0x04)==2`, `read32(0x08)==2`. The console driver requires the identical magic
and version at `:101-102` — only the device id differs (3 vs 2). So the global is
required for it too, and this is a *stronger* requirement than the block driver's in
one respect: the block driver's `read32(0x04) == 2` and the console's are the same
check, so if legacy mode broke the block driver it breaks the console identically.

Both emitters already do this, and neither duplicates it:

- `run-qemu.py:199` sets it for the `--os-root` path.
- `unixnu_boot.py` sets it at `:1770` (systemos), `:1860` (root) and `:1489` (agent
  channel), with `:1790`'s `emit_legacy_global=(root is None and systemos is None)`
  ensuring the agent channel emits it only when neither volume path already did.

### 6f. What this changes about the §5 diff

Nothing in §5 is wrong, and nothing in it needs to grow:

- The two-line diff (one `board.toml` archives entry, one defconfig line) remains
  sufficient **for the build**.
- It does **not** need to touch the ADT, `make-qemu-adt.py`, `make-dt.py`,
  `board.toml [payload.adt]`, or `run-qemu.py`. The ADT already carries all 32 windows
  and QEMU assigns the device into one of them (6c).
- On this host the `virtio-serial-device` is **already in the argv** whenever the agent
  channel is requested (`unixnu_boot.py:1496`), with the chardev at `:1493-1494` and the
  `virtconsole` at `:1497`. So the remaining gap is purely the kernel-side invocation
  §3c records — exactly as §5 says, and no more.

### 6g. UNKNOWN (3)

- **`read32(0x08) == 3` at the assigned window.** Verified by precedent
  (`NativeVirtioBlock.cpp:506` on the same transport), not measured here — no way to
  read a guest MMIO register without a full boot.
- **Whether an *unused* transport reads device id 0**, so the driver cannot
  false-match on one of the 28 idle windows. **Inferred from the lead's own
  measurement (b)**: `native_virtio_input_devices = 2` with tablet + keyboard attached
  out of 32 windows, so idle windows do not spuriously bind for the input driver. Not
  measured for the console specifically.
- **Whether the guest enumerates all 32 `IOPlatformDevice` nubs.** Unmeasured. It is
  XNU's standard behaviour and the block driver's binding proves the mechanism works,
  but the count itself has not been observed.

---

## 7. Summary of the missing-input list

**Missing everywhere (3):**
- `scripts/build-boot-progress.py` — referenced once, at `build-xnu-4k-baseline.sh:70`. R9e.
- `kits/sheng` — referenced once, at `build-arm-driver.py:86` as an `-I` for `NativeAdreno`.
  `vendor/UniXNU/kits/` holds six kits and `sheng` is not one of them; it belongs to the *sheng*
  device repository, which is not this board's `ctx.device`, so `tree.py:66` never creates it.
- `build/sheng-gmu` — referenced once, at `build-arm-driver.py:87`, also an `-I` for `NativeAdreno`.
  A build output with no producer anywhere in either pinned tree.

The last two only matter to `NativeAdreno`, which is itself dormant, so they are latent rather than
live. They are listed because they are the same shape as the console question and a future probe
that only asks "is the file there" would file them under a different heading.

**Present in the pinned tree but never invoked (4):** the archive exists only if something runs
its builder, and for these four nothing does.
- `build-arm-driver.py NativeVirtioConsole` → `build/native-virtio-console/libNativeVirtioConsole.a`
  (`:667-668`)
- `build-arm-driver.py NativeUfs` → `build/native-ufs/libNativeUfs.a` (`:593-594`)
- `build-arm-driver.py NativeAdreno` → `build/native-adreno/libNativeAdreno.a` (`:601-602`)
- `build-arm-driver.py NativeOfflineGPU` → `build/native-offline-gpu/libNativeOfflineGPU.late.a`
  (`:649-650`), which additionally requires the console archive by digest at `:660-661`

**Present, invoked, and fine:** everything in §2a, §2b, §2c, and the "yes/yes" rows of §2d and §2e.

**UNKNOWN (3), stated rather than guessed:**
- whether `NativeVirtioConsole.cpp` compiles clean under `build-arm-driver.py:66-68`'s `-Werror` set;
- whether `make-qemu-adt.py:103`'s `--virtio-mmio-fdt` would publish a `virtio,mmio` console node;
- whether `run-qemu.py` needs an explicit `-device virtio-console-device` for the guest to see one.

---

## 8. The guard, and what mutating it found

`tests/unit/test_kernel_archive_builder_guard.py` generalises
`test_boot_progress_config_guard.py` from "the script is missing" to "the script is
present but nobody runs it". Three tests:

1. `test_the_archive_table_describes_the_build_script` — reads all 16 rows back out of
   `build-xnu-4k-baseline.sh`: the archive path must appear verbatim, and some `= 1`
   guard must test the flag's variable **with the archive inside its own branch
   body**. R9e's guard failed on its own first run because the build script
   *renames* the variable before testing it, so both spellings
   (`${XNU_...:-0}` inline and `$NAME` after a rename) are accepted. The
   branch-body requirement is what stops a row mapping a flag to an archive it
   never links: the console has two `= 1` guards — `:197` installs a CFLAG,
   `:666` requires the archive — and accepting either would let the row name the
   wrong one. Verified 16/16 rows resolve to a guard whose body names its archive.
2. `test_every_enabled_archive_flag_has_an_invoked_builder` — for each enabled key in
   each defconfig, the builder must be invoked. Eleven of `qemu_root_defconfig`'s
   enables resolve, which is what makes this a positive control rather than a
   vacuous pass.
3. `test_the_dormant_keys_are_not_enabled_and_stay_that_way` — no defconfig may enable
   a dormant key, and the dormant set the tree actually has must *equal*
   `DORMANT_KEYS`.

Ten mutants, run in the foreground against the real tree:

| Mutant | Result |
|---|---|
| M1 defconfig enables `CONFIG_XNU_NATIVE_VIRTIO_CONSOLE=y` | **killed** (tests 2 and 3) |
| M2 `board.toml` gains `"build-arm-driver.py NativeVirtioConsole"` | **killed** (test 3) |
| M3 a table archive path gains a typo | **killed** (test 1) |
| M4 an enabled key is mapped to a dormant builder | **killed** (tests 2 and 3) |
| M5 the corpus stops excluding `build-arm-driver.py` | **survived** — see below |
| M6 a table key is renamed off the build script | **killed** (test 1) |
| M7 the console is dropped from `DORMANT_KEYS` | **killed** (test 3) |
| M8 the detector answers "no" to everything | **killed** (tests 2 and 3) |
| M9 the detector answers "yes" to everything | **killed** (test 3) |
| M10 the console is mapped to another flag's archive | **killed** (test 1) |

**9 of 10 killed; the survivor is M5 and it is not a hole.**

**M2 and M7 survived the first revision and were real holes.** M2 because the second
assertion was "the dormant set is non-empty", which the *other three* dormant keys
satisfied — so wiring up the console changed nothing observable. M7 because deleting
the console from `DORMANT_KEYS` left the set non-empty and therefore silent. Both are
fixed by making the dormant set a derived **equality**: compute it from the table and
the corpus, and require it to equal `DORMANT_KEYS`. Now a key that gains a builder and
a key that loses one are both red.

**M5 survives and is not a hole.** The exclusion of `build-arm-driver.py` from the
corpus is redundant, and the mutant is how that was found rather than assumed:
`build-arm-driver.py` contains **zero** lines naming both itself and a component
(its choices list at `:11`, its output map at `:31` and its include line at `:81` each
name components but never the script's own filename). The two-token requirement in
`_is_invoked` is what actually rejects them. The exclusion is kept as depth against a
future docstring, and the comment on `DRIVER_SCRIPT` now says so instead of claiming a
necessity that was not measured.

The first revision of the test also failed on its own transcription twice before it
went green — once for `f"${variable}"` producing `$XNU_...` without the brace the
script actually spells, and once for a prefix collision where `XNU_NATIVE_AES` matched
`${XNU_NATIVE_AES_ARCHIVE:-...}` and read the archive-path override as a rename of the
flag. Both are fixed with a word-boundary regex, and both are recorded here because
they are the same failure R9e recorded: a hand-transcribed table that no longer
describes the build.


---

## 9. What was run, and how the two suite failures were attributed

| Run | Result |
|---|---|
| `pytest tests/unit/test_kernel_archive_builder_guard.py -q` | **3 passed** |
| `pytest tests/unit/test_kernel_archive_builder_guard.py test_boot_progress_config_guard.py -q` | **5 passed** (the new guard does not disturb the existing one) |
| `pytest tests/unit/test_kernel_archive_builder_guard.py test_boot_progress_config_guard.py test_qemu_metal_handoff_guard.py -q` | **27 passed** |
| Mutation harness, 10 mutants, foreground | **9 killed**, M5 survived (redundant depth, not a hole) |
| `pytest tests/unit -q`, first full run | **1 failure**, `test_qemu_metal_handoff_guard.py::test_the_scanout_client_probes_for_a_selector_instead_of_assuming_one` |
| `pytest tests/unit -q`, second full run | **1 failure**, `test_controller_cli.py::test_doctor_is_structurally_stable_across_invocations` |

**Neither failure is this change's, and both were attributed rather than assumed.**

The first is a concurrent edit, not a defect. `vendor/UniXNU-qemu/package/QemuMetal/QemuMetalScanout.mm`
was written at **06:17:01**, inside the window of the run that failed — another agent in this
session was mid-edit on the QemuMetal hand-off, and the guard read the file mid-write. The
same test passes both in isolation and in the second full run, and the QemuMetal work is
another agent's (its files show as modified in `git status` while this change's two files are
untracked additions).

The second is the documented disk-churn flake. `test_controller_cli.py`'s own comment states
the mechanism: it normalises `free_gb` away because "any other test writing ~100 MiB between
these two invocations moves `free_gb`". The full suite writes a large `pytest-of-*` basetemp,
and F: is at 97% used with ~21 GiB free, so free space moves between the two `doctor`
invocations. It passes in isolation.

Both are the known "parallel teammate churn" pattern: the suite fails on files this change
never touched, so the failure has to be attributed to a cause before it is reported as one.
