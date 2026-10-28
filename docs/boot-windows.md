# Windows 本机启动链：实测记录

- 日期：2026-10-24（2026-10-28 修订：下面「内核」一行原本写的是更早那一束，已按当前束的
  `manifest.json` 改正；§1.4 的串口摘录仍是**更早那个内核**上量到的，未改写）
- 主机：Windows 11，QEMU **11.1.0**（`local-private/tools/qemu/qemu-system-aarch64.exe`，sha256 `952f8975…`），
  固件 `local-private/tools/qemu/share/edk2-aarch64-code.fd`（67,108,864 字节，sha256 `47765fe3…`）
- 内核：`local-private/boot32/bundle` 束内
  `unixnu-out/qemu/kernel/kernel.macho`，**125,431,808 字节**，sha256 `e6480f661dfa4efd…`
  （该束 `manifest.json` 的记录；同束 `DarwinBoot.efi` 27,136 字节 sha256 `f78ad56a6635a710…`、
  `UniXNU.efi` 93,696 字节 sha256 `59b7aa748848328b…`、trustcache 61,356 字节
  sha256 `7416ca4f83771466…`——与 `vendor/UniXNU-qemu/board.toml` 的第一条
  `trust_cache_sha256` 一致，所以这是一套**匹配的构建**）
  更早那一束是 51,859,456 字节、sha256 `9d4b1ab14d74fdfa…`（runner run `40096928333`），
  再往前一束是 `1f32f09bc7668de0…`（run `40081850615`）。**一次启动日志属于哪个内核，
  只能由束里的 `manifest.json` 说**（见 §1.4）——这一行写错过一次，本机实际启动的早已不是
  它描述的那个内核，而症状是全套启动测量看起来仍然成立。
- 设备树：`local-private/boot32/bundle/kernel.macho.adt`，17,768 字节，**本机生成**（见 §3），
  `unique-chip-id` `02723f6de1295be8`
- 代码：`src/unipad/host/adt.py`（生成 ADT）、`src/unipad/host/unixnu_boot.py`（搭 ESP 并启动）、
  `src/unipad/host/guestobserve.py`（读活访客的内核日志与进程表）、
  `src/unipad/host/udif.py`（把 UDIF 容器解成裸镜像）

本文件的职责是把**已证明的**与**未证明的**分开写。构建阶段名称、Actions 绿色、二进制格式正确，
都不等于启动验收通过；反过来说，启动验收通过也不等于用户态跑起来。

---

## 1. 已证明（都在本机实测）

### 1.1 机器与固件

```
UEFI firmware (version edk2-stable202408-prebuilt.qemu.org built at 16:28:50 on Sep 12 2024)
BdsDxe: starting Boot0001 "EFI Internal Shell" ...
UEFI Interactive Shell v2.2 / EDK II / UEFI v2.70
Shell>
```

`virt,virtualization=off,secure=off,gic-version=3` + `-cpu cortex-a710` + `-accel tcg` + `-m 6144` +
`-smp 1`，只读 code flash 与可写 vars flash（截断到 code flash 大小）这一对，在 Windows QEMU 11.1.0 下
正常启动 EDK II 并到达交互 Shell。`ramfb`、`qemu-xhci`、`usb-kbd` 均被接受。

### 1.2 ESP 能被固件完整读取

用 UEFI Shell 的 `startup.nsh`（放在 ESP 根目录，Shell 启动后自动执行）实测：

```
Shell> ls FS0:\EFI\BOOT
Directory of: FS0:\EFI\BOOT\
10/24/2026  18:59 <DIR>         8,192  .
10/24/2026  18:59 <DIR>         8,192  ..
10/24/2026  18:59              14,336  BOOTAA64.EFI
```

即：`fat:<目录>` 形式（QEMU 的 vvfat）在 Windows 上可用，**含 51,859,456 字节的 `kernel.macho`**
（Python 解析 vvfat 合成卷：FAT16、512 B/扇区、16 扇区/簇，`KERNEL~1MAC` size 51859456）。
也验证了把同一份内容导出为裸镜像后行为一致——问题不在 vvfat。

### 1.3 QMP 与串口日志链路

`unipad.host.unixnu_boot.boot()` 实测：TCP QMP（Windows 无 `AF_UNIX`）建连成功，
`query-status` 返回真实的 `{"status": "running", "running": true}`；`serial.log` 落盘；
`screendump` 每个 `--every` 秒产出一张 PNG（`_write_png` 自己写 PNG，无第三方依赖）。
`boot-report.json` 记录完整 argv 与输入清单。

**两处更正，都是本轮实测逼出来的：**

1. 本节最初写"收到 greeting 与 `query-status` 回复"，那句话**过头了**。`_qmp` 当时在请求循环里
   `return`，于是它在 `qmp_capabilities` 的回复后就返回，**从来没有把调用方给的命令发出去**——
   `query-status` 没发、`screendump` 没发、`quit` 也没发，每次运行都落到 `process.terminate()`，
   而报告里记的是一条"没人问过的问题的回复"。修正后 `query-status` 的回复如上，截图链路才第一次
   真的出图。这也是"测试全绿不等于它跑过"的一个具体例子。
2. `screendump` 第一版传的是**相对路径**，QEMU 以自己的 cwd（运行目录）解析，于是去找
   `run1/run1/screen.ppm`，报 `Could not create ...: No such file or directory`。与 argv 里固件
   路径那一处是同一类错误；改成绝对路径 + 正斜杠后出图正常。

### 1.4 UEFI → UniXNU → DarwinBoot → XNU 交接：已完成（2026-10-24 13:25 实测）

用 runner run `40096928333` 的 `boot-inputs` 束（`UniXNU.efi` 93,696 字节、PE subsystem **10**、
sha256 `cc39d615fa44d255…`；`kernel.macho` 51,859,456 字节、sha256 `9d4b1ab14d74fdfa…`；
`DarwinBoot.efi` 27,136 字节、sha256 `a3b7065c6d020414…`）加本机生成的
`kernel.macho.adt`（17,624 字节，`unique-chip-id` `02723f6de1295be8`）启动。串口全文：

```
UNIXNU: UniXNU 0.1.0 starting as application, firmware EDK II rev 0x10000
UNIXNU: config: loaded \EFI\UniXNU\unixnu.toml
UNIXNU: config: 1 entries, 1 drivers, 0 dt patches, 0 acpi tables, 0 slices, 0 kits loaded, 0 skipped
DARWINBOOT: loader v2 installed EL1
UNIXNU: driver \EFI\UniXNU\Payloads\DarwinBoot.efi (config): started
UNIXNU: ui: graphics 800x600, scale 1, landscape layout
UNIXNU: menu: shown 1 entries, default 0 'xnu', timeout 3000 ms, input yes, rotate 0 ms
UNIXNU: menu: timeout, booting default 0
UNIXNU: boot: entry 'xnu' (QEMU virt: source 4K XNU)
UNIXNU: xnu: handing off 0 staged kext(s) to loader
DARWINBOOT: ARM64 va=0xFFFFFFF00427C000 pa=0x4807C000 args=0x4C170000 entry=0x4B1EC488 owned=0x100000000
DARWINBOOT: video base=0x1BC7A0000 display=1 row=3200 width=800 height=600 depth=32
DARWINBOOT: inputs validated; exiting boot services
DARWINBOOT: hand-off to the kernel
kernel   va 0xFFFFFFF00427C000  pa 0x4807C000
entry    pa 0x4B1EC488
args     pa 0x4C170000  window 0x100000000 bytes
video    base 0x1BC7A0000  800x600  row 3200 bytes  32 bpp
entry words 140006F5 00000000 00000000 00000000
CurrentEL 1  SCTLR_EL1 0x30D0198D  TCR_EL1 0x280803518
ID_AA64MMFR0_EL1 0x22200101122  (PARange 2, 4 KiB granule supported)
ID_AA64PFR0_EL1  0x1201001121110012
Exiting boot services. A green bar across the top means the kernel entry code ran.
```

交接之后串口不再增长、帧缓冲也不再变化。为判断"内核在安静运行"还是"卡死"，用 QMP
`info registers` + 帧链走了客体的调用栈（`local-private/boot7/callstack.py`，只读）：

```
PC   _ml_get_timebase+0x14
LR   _processor_idle+0x174
ret  _idle_thread+0x44
ret  _Call_continuation+0x44
```

**CPU 在 XNU 的空转线程里。** 也就是说内核不只是"入口代码跑了"，而是一路走到了调度器并进入空闲
循环——这是一个健康的、已启动完成的内核，不是挂死。PC 在 50 秒内逐字节不变、`PSTATE` 为 EL1t，
与空转循环一致。

截图（`local-private/boot7/run2/screen-150s.png`）定格在 UniXNU 的启动菜单与倒计时上：
帧缓冲里仍是 UniXNU 自己画的内容，内核没有往上画任何东西。

---

## 2. 第一个阻塞点（已定位，证据充分）

**缺 `UniXNU.efi`——`EFI/BOOT/BOOTAA64.EFI` 必须是 UEFI *application*，而手里只有 driver。**

实测：把 `DarwinBoot.efi`（27,136 字节）或 `DarwinBootValidation.efi`（14,336 字节）放到
`EFI/BOOT/BOOTAA64.EFI`，固件的反应是

```
Shell> \EFI\BOOT\BOOTAA64.EFI
The image is not an application.
```

以及 BDS 的

```
BdsDxe: failed to load Boot0001 "UEFI Misc Device" from PciRoot(0x0)/Pci(0x1,0x0): Not Found
```

第二条消息只点名设备、不点名原因，看起来像"文件找不到"——而 `ls` 证明文件在、也能打开。

PE 头解析给出了确定原因：

| 文件 | machine | magic | subsystem |
| --- | --- | --- | --- |
| `DarwinBoot.efi` | 0xAA64 | PE32+ | **11**（EFI boot service driver） |
| `DarwinBootValidation.efi` | 0xAA64 | PE32+ | **11** |
| `vendor/UniXNU/package/UniXNUPkg/Application/UniXNU/UniXNU.inf:13` | — | — | `MODULE_TYPE = UEFI_APPLICATION` → subsystem **10** |

DarwinBoot 本来就是被 UniXNU 当 driver `LoadImage` 的（`board.toml [payload] loader = "driver"`），
不是被固件启动的镜像。上游 `run-qemu.py:93-95` 放在 `EFI/BOOT/BOOTAA64.EFI` 的是
`package/Build/Out/AARCH64-DEBUG/UniXNU.efi`。

`UniXNU.efi` 由 `vendor/UniXNU/package/build.sh` 生成，需要 mu_basecore EDK2 检出（含
BaseTools 从源码构建）与 AArch64 clang；macOS runner 的 `build-bootloader` 步骤**已经跑过**
`unixnu build package`，但产物收集只取了 `unixnu-out/qemu/kernel/kernel.macho` 与
`darwinboot-efi/`，没有收集 `package/Build/Out/AARCH64-DEBUG/UniXNU.efi`。这是收集遗漏，不是没构建。

**字节已经不存在了。** `local-private/b5/artifact-manifest.json` 里有它的记录
（93,696 字节，sha256 `59b7aa748848328bc88d363d57180c9faba491eccfd5e7847565e033d4858016`），
但承载它的 350 MB 整树 artifact 在公开暴露清理时被删除，本机也没有副本——`boot.zip`
（14 MB）只是 `boot-inputs` 束，只含 kernel 与 darwinboot。所以只能重新构建，不能恢复。

改正（已写入仓库，**并已在 runner run `40096928333` 上验证**）：

1. `scripts/macos/build-macos.sh` 的 `step_build_bootloader` 新增两步：
   glob 出 `AARCH64-*/UniXNU.efi`（缺一个 `die`，多一个也 `die`——"两个候选却说不出哪个会启动"
   本身就是证据不完整），然后读 PE subsystem，**不是 10 就记 `failed`**。
   复用 `unipad.host.unixnu_boot.pe_subsystem`，这样 runner 与 Windows 本机不可能对
   "固件会怎么对待这个文件"产生分歧。三种结局都实测过：DarwinBoot.efi → FAIL(11)、
   打补丁的孪生体 → PASS(10)、设备树 → FAIL(不是 PE32+)。
   runner 上的实测输出：`bootloader: UniXNU.efi 93696 bytes, PE subsystem 10 (EFI application)`。
2. `.github/workflows/build-macos.yml` 的 `boot-inputs` 束把 `UniXNU.efi` 列为**第一项**，
   并新增 `manifest.json`：束内每个文件的 sha256 与字节数（`manifest.json` 不哈希自己）。
   理由是实测出来的——三个历史 `kernel.macho` 并不逐字节相同（M2 `9fc119604838856d…`、
   M4 `1f32f09bc7668de0…`、本机这一次 `9d4b1ab14d74fdfa…`），一份不点名自己内核的启动日志
   是没人能复现的日志。束里也如实记下了 6 个缺席条目（`payload.img`、`kernel.macho.adt` 等），
   因为 `build-payload` 失败了（§2.1）。
3. staging / 大上传 / boot-inputs 三个步骤改为 `if: always()`。run `40094234507` 在
   `build-payload` 失败时把它们全部跳过，于是 `build-bootloader` 已经产出并记录的
   `UniXNU.efi` 既没暂存也没上传——**失败即丢证据**。加上 `always()` 后，run `40096928333`
   同样在 `build-payload` 失败，但束照样上传，`UniXNU.efi` 才第一次到本机。

同时 `unipad.host.unixnu_boot._refuse_non_application()` 在启动前读 PE subsystem，
把上面那条含义不明的固件错误变成一条指名原因的拒绝。

## 2.1 第二个阻塞点（已定位，正在解决）

`build-payload` 失败：`board.toml [payload] panic_screen = true` 让 payload 阶段编译
`scripts/xnu-panic-screen.c`，而它 `#include "Font.h"`（`:294`），该文件来自一个既不是 feed、
也不在两个 pinned 仓库里的 Mu-Silicium 检出。`patch-xnu-entry-probe.py:183` 读 `UNIXNU_UEFI`，
而 `payload.py:139` 给它的是 `ctx.uefi`，payload 步骤里那是 `None`，于是回退到
`<tree>/uefi-sheng`——合成树里从来没有这个目录。

run `40094234507` 的实测死法是 clang 的 `fatal error: 'Font.h' file not found`：指名 include、
不指名原因，与 §2 的固件错误同一种形状。已在 `step_build_payload` 里加了预检，ledger 现在写：

```
payload: FAIL build-output/unixnu-out/qemu/tree/uefi-sheng/Silicon/Silicium/SiliciumPkg/Library/FrameBufferSerialPortLib/Font.h is missing
```

代价只有 panic screen：entry probe（stage 条、`--no-reset`）是同一个 patch 的另一半，
两种设置都要（`payload.py:128-129`），所以不打补丁的内核照样启动——§1.4 用的就是没打补丁的内核。
要补上得让操作员把 `UNIXNU_UEFI` 指向一个 Mu-Silicium 检出；这一步**没有**代替做，
因为它需要上游没有发布的材料，瞒下来比记下来坏。

---

## 3. 设备树（`kernel.macho.adt`）：本机生成成功

`src/unipad/host/adt.py` 按 `scripts/unixnu/payload.py:145-163` 的映射完整传参：

```
python3 <tree>/scripts/make-qemu-adt.py --output kernel.macho.adt \
  --dram-size 6442450944 --owned-memory-size 0x100000000 \
  --root-match-volume-uuid 04769D26-2CA0-4825-B443-E2E5F88300F2 \
  --chip-id 207 --cpu-regs 0 --unique-chip-id <seed> --ipad-identity
```

结果（17,624 字节）：`model = iPad7,1`、`target-type = J120`、`/chosen/chip-id` = 519（0x207）、
`/chosen/unique-chip-id` = `e85b29e16d3f7202`、`cpu0/timebase-frequency` = 62,500,000 Hz
（正是 TCG 需要的值，`run-qemu.py:120-125` 会拒绝不匹配的树）。`arm-io` 下 32 个
`virtio-mmio-*`、`intc`、`defaults`、`product`、`buttons` 齐备；QEMU 的 `ufshc`/`ufsphy`
节点已按 `make-qemu-adt.py:127` 移除。

### 3.1 两个必须记住的坑

1. **`[payload.adt] chip_id` 是十六进制字符串**：`make-qemu-adt.py:68` 用 `int(value, 16)`，
   所以 `chip_id = "207"` 写进树里是 519，不是十进制 207。按十进制比对会在读回时误报不匹配。
2. **树不是字节可复现的**：`vendor/UniXNU/scripts/qemu-entry/make-dt.py:147` 用
   `os.urandom(64)` 写 `/chosen/random-seed`。相同输入两次生成的 sha256 必然不同。
   这是内核的早期熵源，理应随机；因此 ADT 的哈希只能证明"这一份"，不能证明"可复现"。

   实测两次生成（同一条 pin、同一种子）：都是 17,624 字节，235 个节点+属性**结构完全相同**，
   `unique-chip-id`、`chip-id`、`model`、`target-type`、`timebase-frequency` 全部逐字节相同，
   唯一不同的是 64 字节 `random-seed`。也就是说：**结构确定，熵源随机**。

### 3.2 平台种子

`board.toml [payload.adt] unique_chip_id = "required"`，种子按设计不入任何仓库。本机由
`unipad.host.adt.derive_seed(<pinned device commit>)` 从锁定的提交确定性导出：
`sha256(b"unipad/qemu-platform-seed/v1" + 0x00 + ascii(commit))[:8]`。
导出而非随机，是为了同一条 pin 得到同一个客体身份；它不是密钥，没有任何东西由它派生密钥。

### 3.3 两种设备树的区别（不要混用）

| | IPSW 的 `DeviceTree` | `kernel.macho.adt` |
| --- | --- | --- |
| 来源 | Apple IPSW 组件，`unipad.host.ipsw` 提取 | `make-qemu-adt.py` 由 QEMU 自己 dump 的 FDT 加工 |
| 描述对象 | 真实 iPad7,1 硅片 | **QEMU virt 机器** |
| 用途 | 证据：证明镜像是这块板子的 | 启动：XNU 开机时读的就是它 |
| 校验 | `board.toml [ipsw.sha256]` | 读回校验 identity 属性 |

有前者不等于有后者；`build-devicetree`（提取 Apple 的）与 `build-devicetree/adt`（生成本机的）
因此是两个步骤。

### 3.4 树里没有 UART 节点——而且这不是串口安静的原因

`kernel.macho.adt` 共 44 个节点：`chosen`（含 `memory-map`、`manifest-properties`）、`cpus/cpu0`、
`defaults`、`options`、`product`、`arm-io`（`intc` + 32 个 `virtio-mmio-N` + `buttons`）。
**没有任何一个叫 `uart`/`pl011`。** 这不是遗漏：`vendor/UniXNU/scripts/qemu-entry/make-dt.py:172-186`
是**从零合成**这棵树的，`--armvirt` 时 `ranges` 写的是 VirtIO MMIO 区（`0, 0x08000000, 0x04000000`），
不写 PL011 的 `0x09000000`；`make-qemu-adt.py` 只补平台种子与 iPad 身份、删掉 UFS 节点。

于是做了一个实验（`local-private/boot7/uart_experiment.py`）：给 `arm-io` 加一个
`compatible = "arm,pl011"`、`reg = <0 0x09000000 0 0x1000>`、`interrupts = <0 1 4>` 的 `uart` 节点，
树从 17,624 字节变成 17,924 字节，再启动。**串口输出完全没有变化**，仍是交接后的 2,060 字节。

结论：**缺 UART 节点不是串口安静的原因，因此也不该为此改动上游生成的树。** 真正的原因待查（§5.2），
而上游 `run-qemu.py:304-306` 自己只打印 `UNIXNU:`/`DARWINBOOT:` 前缀的行——它**本来就不期待**
内核往串口写东西。所以"安静"很可能就是预期行为。

---

## 4. 未证明 / 阻塞

| 项 | 状态 | 原因 |
| --- | --- | --- |
| UEFI → UniXNU 交接 | **已证明** | §1.4 串口 + PE subsystem 10 |
| UniXNU → DarwinBoot 交接 | **已证明** | §1.4 `DARWINBOOT: loader v2 installed EL1` |
| DarwinBoot → XNU | **已证明** | §1.4 `hand-off to the kernel` + 寄存器转储 |
| 内核进入用户态 / SpringBoard | **BLOCKED** | 见 §5 与 §6：OS root 卷已解出且 UUID 匹配，但本机内核无 `CONFIG_XNU_NATIVE_VIRTIO_BLOCK`，第二个盘从未被枚举 |
| 根卷挂载（第二个 virtio-blk） | **BLOCKED** | §6：缺的是内核配置，不缺镜像；挂载侧已完整验证 |
| 内核串口输出 | 未观察到 | §3.4：**不是缺陷**，与上游一致；加 UART 节点的实验无效 |
| `QemuRamfb.efi` 设置 1376x1024 | **已测量，不是修复** | 见 §7.4：驱动确实被 staging 并运行（内容确定性地变了），但几何仍是 800x600，且画面依旧从不更新 |
| 十条端到端验收 | BLOCKED | 同上 |

**没有任何一项被写成"通过"以外的话。** 构建阶段名称、Actions 绿色、PE/Mach-O 格式正确，都不代替启动验收；
反过来说，启动验收通过也不等于用户态跑起来——§1.4 证明的是交接，不是 iPadOS。

## 5. 下一条实验假设

### 5.1 用户态：OS root + shared window + QemuMetal

内核已进空闲线程，但没有任何用户态。上游 `run-qemu.py` 给出的路径是三个东西一起上：

- `--os-root`：**上游自己的开关**（`vendor/UniXNU-qemu/scripts/run-qemu.py:36`，`action='store_true'`，
  **不带路径**）。它让 `run-qemu.py:199-208` 先设 `-global virtio-mmio.force-legacy=false`，再把 IPSW 里的
  OS 镜像与 SystemOS 镜像各挂成一个只读 `virtio-blk-device`（`drive=os` / `drive=systemos`）。
  镜像本身由 `scripts/build-os-root.py` 从**已在本机的**
  `local-private/ipsw/iPad_Pro_HFR_17.7.11_21H461_Restore.ipsw`
  （6,835,124,840 字节）提取。**本仓库没有 `--os-root` 这个选项**，别在 `unipad` 或探针上找它：
  对等的写法是 `unipad.host.unixnu_boot.build_argv(..., root_image=...)`，以及
  `scripts/windows/probe-root-volume.py --root-image PATH`（§6）；
- `--shared-window`：`memory-backend-file` 的共享内存窗（上游注释记录：**共享全部客体内存会让用户态
  停在 launchd 上空转**，所以只共享这一个窗）；
- `--metal`：每个 slot 一个 `metal-host` 进程，`package/QemuMetal/QemuMetalPublish.cpp` 发布授权。

`build-os-root.py` 的十步里，第 4 步要用 `hdiutil` 挂载卷（macOS 专属），第 10 步要原始 trust cache
（AMFI 没有它拒绝跑 launchd）。因此这一步**必须在 macOS runner 上做**，Windows 只能做准备与验证。
它同时是 PRD §12 的敏感路径：提取出来的是 Apple 的 OS 内容，只能留在 `local-private/`，
上传只允许本项目自己生成的产物。

本机已经能做的那一半：UDIF 容器可以用 `src/unipad/host/udif.py` 解成裸镜像——解开 `044-76040-038.dmg`
这步不需要 `hdiutil`（上游是 `build-os-root.py` 自己调 `hdiutil` 挂载目标卷）。**但解出镜像并不等于
能挂上**——§6 实测，本机内核枚举不了第二个块设备，所以 `--os-root`/`root_image=` 这一条现在卡在
内核配置上，不在镜像上。

### 5.2 观察手段：先解决"看不见"

串口在交接后安静、帧缓冲不变，所以目前**没有任何东西能告诉我们用户态发生了什么**。
在把 OS root 拉起来之前，值得先确认 XNU 的控制台在哪：

- 已排除：ADT 缺 UART 节点。实测给 `arm-io` 加一个 `arm,pl011`（0x09000000、SPI 1）后串口
  输出**完全没有变化**（`local-private/boot7/uart_experiment.py`），所以那不是原因，
  也不该为此改动上游生成的树。
- **内核层已经看得见了**：`unipad.host.guestobserve` 从 `msgbufp` 环形缓冲反卷出内核日志
  （§6 用的就是它），所以"内核到没到挂载点"不再依赖串口。剩下的盲区只剩**用户态**。
- 待查：帧缓冲。内核拿到的是 `0x1BC7A0000`/800x600/32bpp（ramfb），但 XNU 的 console 可能画在
  另一个 surface 上，或者 `debug=0x14e` 位没有打开串口输出。
- 待查：`run-qemu.py` 自己只打印 `UNIXNU:`/`DARWINBOOT:` 前缀的行——上游**本来就不期待**内核串口
  输出，所以"安静"可能是预期行为，而不是缺陷。这一点在把 OS root 拉起来之前必须确认，
  否则用户态出了问题同样看不见。

### 5.3 帧缓冲几何（已知限制，不是缺陷）

`-fw_cfg name=opt/org.unixnu/ramfb-size,string=1376x1024` **只在 ESP 上真有 `QemuRamfb.efi` 时才发**
（`unixnu_boot.boot()` 检测后传给 `build_argv`），因为该 fw_cfg 项就是由那个驱动读的
（`run-qemu.py:105-116`）。本机现搭的 ESP 不带该驱动，所以帧缓冲是固件默认的 800x600，
§1.4 的截图比例与 iPad 不符。**不能据此判断渲染是否正常。**

---

## 6. 根卷挂载实测：缺的是内核配置，不是镜像（2026-10-25）

`scripts/windows/probe-root-volume.py` 把 OS root 卷作为**第二个**块设备挂上、启动真客体，
并在 QEMU 运行期间通过 `unipad.host.guestobserve` 读活访客。它回答的只有一个问题：
一个 `qemu_defconfig` 的 UniXNU 内核，到底能不能枚举第二个 virtio-blk 设备、能不能挂上根卷。

```
python scripts/windows/probe-root-volume.py --root-image PATH [--seconds N] [--settle S] [--run DIR] [--decode-udif]
```

### 6.1 它测的就是 launcher 会启动的那台机器

ESP 由 `unipad.host.unixnu_boot.stage_esp` 现搭，`vars.fd` 截断成**全零**（不是固件的字节拷贝）——
与 `boot()` 完全一致。早先一版是 `copytree` 上一次运行的 ESP、再把固件字节写进 `vars.fd`，
那是另一个 EDK II 变量存储、因此是另一台机器，而在本探针读的串口日志里看不出区别。
argv 由 `build_argv(..., root_image=root)` 生成，不重新实现。

拒绝都发生在 QEMU 启动之前：镜像不可用（2）、板级内存与本模块给的机器不一致（2）、
`stage_esp` 写出的 payload 文件数不是 5（2）、UDIF 解码被拒（3）。
客体会话本身：没到内核交接返回 4，截止时间到了仍无标记返回 5——**早先一版放弃时返回 0**，
于是"失败"与"成功"在进程层面无法区分，实测 306 秒、44 次观测、exit 0，而客体从没到过挂载点。

设备树取自己搭过的 ESP（`build-payload` 因缺字体头失败，束里没有新的树），所以每次运行都
**按 `board.toml [payload.adt] root_match_volume_uuid` 校验这棵树**，对不上就退出：
一份过期树会让探针去测一个错的 UUID。

镜像按 `board.toml [ipsw.sha256]` 的摘要点名，并按 `run-qemu.py:186`/`:196` 的角色标注；
挂成 SystemOS cryptex 时告警——根匹配 UUID 属于 `os` 成员，挂它**无论内核怎样都不可能成功**。

### 6.2 两个盘必须在不同传输层

ESP 留在 **PCI**（`virtio-blk-pci`）：EDK II 要枚举虚拟 PCI 总线才能找到并挂载它
（`BdsDxe: loading Boot0001 ... from PciRoot(0x0)/Pci(0x1,0x0)`）。

根卷必须是 **MMIO**（`virtio-blk-device`），这不是偏好：会读它的驱动按传输层匹配。
`vendor/UniXNU/drivers/NativeVirtioBlock/NativeVirtioBlock.cpp:148-154` 的 `probe()` 在 provider 的
`compatible` 不是恰好 12 字节 `"virtio,mmio"` 时直接 `return nullptr`，所以 `virtio-blk-pci` 设备
**无论内核怎么配都不可能被它绑定**。第一版 `build_argv` 的 `root_image` 把根卷也写成了
`virtio-blk-pci`，而 QEMU 毫不抱怨地接受了这条 argv——跑得起来、日志正常，只有去读那个永远不会
绑定它的驱动的 `probe()` 才发现错。

还必须带 `-global virtio-mmio.force-legacy=false`（上游 `run-qemu.py:199` 同样如此）：不带这个 global，
MMIO 传输留在 legacy 模式，寄存器偏移与驱动期待的不一致——`start()`
（`NativeVirtioBlock.cpp:499-506`）要求 provider 带 `dma-coherent`、
`mapDeviceMemoryWithIndex(0)` 长度 ≥ `0x108`，并读 `read32(0x00)==0x74726976`（"virt"）、
`read32(0x04)==2`、`read32(0x08)==2`。

### 6.3 内核日志不在串口上

串口在 DarwinBoot 的 `hand-off to the kernel` 之后就不再增长（§1.4），所以标记只能从 msgbuf 里读：
`unipad.host.guestobserve` 反卷 `msgbufp` 环形缓冲。轮询的标记是 `Waiting on` / `bsd_autoconf` /
`mountroot` / `IOMedia`，并记录用了几次观测才到。

这同时是一条方法学教训：第一次运行只在内核加载行后等 3 秒，日志只有 10,985 字节、`allproc` 还空着，
看起来像「挂了盘反而更早失败」，其实是采样太早。**这条测量必须等内核走到挂载点才有意义。**

### 6.4 UDIF 容器先解码：`src/unipad/host/udif.py`

`044-76040-038.dmg`（5,157,406,069 字节）是 **UDIF 容器**，不是裸 APFS。QEMU 的 `format=raw`
打不开它，而且**不会抱怨**——它会把这个压缩容器当裸盘交给客体，于是没有 GPT、没有 APFS、
没有卷 UUID，启动停在和不挂盘时一模一样的 `Waiting on IOMedia` 上，而调用方看着
`report.root_image` 已设、`main()` 打印 "root volume attached"。所以 `build_argv` 直接拒绝带
`koly` 尾部的镜像，并指名去解码；探针则自己解。

`udif.py` 读容器、写裸字节：`koly` 尾部在 `filesize-512`（全大端），XML plist 的
`resource-fork/blkx` 每项 `Data` 是 204 字节 `mish` 头加 `NumberOfBlocks` 个 40 字节块记录
（`type`@0、**相对扇区**@8、扇区数@16、**压缩偏移**@24、压缩长@32）。`sectorNumber` 是
**相对本 entry** 的——这是这个格式里唯一容易搞错的一处偏移，上游自己的
`extract-target-volume.py` 也这么读。块类型：`0x80000007` LZFSE 3415 个、`0x00000001` raw 396 个、
`0x00000002` 零填充 3 个、`0xFFFFFFFF` 终止符。

它**不解 LZFSE**：Python 标准库没有 LZFSE 解码器，而 OS 成员 3814 块里 3415 块是 LZFSE。
解析是完整的、无需库的两种编解码（raw 与零填充）在本模块内，所以调用方给解码器
（`LzfseDecoder(decode)`）就拿全镜像，不给就拿一条说清缺什么的拒绝。半解码静默产出一份错镜像更糟：
GPT CRC 虽然能抓到，但那是 6 GB 写完之后的事。API：`UdifImage(path)`、`needs_lzfse()`、
`chunk_types()`、`chunks()`、`chunk_bytes(chunk, lzfse)`、`write_raw(destination, lzfse, progress=None)`、
`check_gpt(image)`。

`write_raw` **失败不留任何东西**：可恢复的失败曾把目的地变成一整个全尺寸半成品，之后每次重试都被
"先删掉它"挡住。它有 `progress` 回调，因为 LZFSE 路径是瓶颈且差两个数量级——本机实测约 **2 MB/s
输出**，6 GB 约一小时，而 I/O 一侧约 31 秒；静默的一小时与卡死无法区分。

UDIF 与否靠**嗅探 `koly` 尾部**判断，不看 `.dmg` 后缀：`044-76034-038.dmg` 是 3,303,014,400 字节
裸 APFS、根本没有尾部，`044-76040-038.dmg` 才是 UDIF 容器。按后缀猜的第一版恰好拒绝了那个
唯一不需要解码的镜像。

### 6.5 结果

- **镜像就是客体等的那一个。** `044-76040-038.dmg` 解出 **6,077,580,800 字节**（11,870,275 扇区，
  6 个 entry，entry 3 从扇区 34 起、11,870,208 扇区、3809 块），GPT 头 CRC32 `0xd9e4279c` 与
  分区项 CRC32 `0xfa7a560c` 双双匹配——这是解码字节精确的证明。分区只有一个 APFS
  （卷名 `DawnUpdate21H461.J120J121J207J208OS`），其卷超级块在 +0xf0 处正是
  `04769d262ca04825b443e2e5f88300f2`，与内核 `Waiting on ... IOMedia ... UUID 04769D26-...`
  完全一致。
- **挂载侧代码完整且已验证。** QMP `query-block` 报 `device=osroot inserted=True`（只读，
  backed by 该镜像），`esp` 仍是独立设备。修正传输层后复测：QEMU 接受 argv（stderr 空），
  访客走到同一点（17,113 字节内核日志、`bsd_autoconf` 在、仍在等同一个 UUID）。
- **内核仍然挂不上。** 整个内核日志里检索 `virtio` / `block storage` / `IOMedia` /
  `AppleDiskImage` / `disk0` / `apfs`（`install_apfs_vfs_fs` 之外）/ `GPT` / `partition`，
  **除 `Waiting on` 那一行本身外零命中**——第二个盘从未被枚举成任何 IOMedia。
  原因是配置而不是运气：`vendor/UniXNU-qemu/configs/qemu_defconfig`（本机**全部五个**内核构建都用它）
  **没有** `CONFIG_XNU_NATIVE_VIRTIO_BLOCK`；`qemu_root_defconfig` 才加它，并且同时要
  `CONFIG_XNU_VERIFIED_ROOT_PROBE`、`CONFIG_XNU_OWNER_ROOT_AUDIT/TRUST/CACHE_MOUNT/CACHE_STARTUP/
  BOOT_MANIFEST/UNION_SYSTEMOS`、`CONFIG_XNU_NATIVE_RAM_BLOCK`、`CONFIG_XNU_OWNER_DATA_MOUNT`、
  `CONFIG_XNU_NATIVE_LEGACY_DISPLAY`，以及两个在内核步骤之外产出的归档。
  全盘 `local-private/**/kernel.macho` 共 24 个文件、按 sha256 去重后 **5 个真构建**（四个 51,859,456
  字节、一个 46,415,872 字节；其余 13–73 KB 的是占位桩不是内核），逐个检索
  `NativeVirtioBlock`/`virtio,mmio`/`CONFIG_XNU_NATIVE_VIRTIO_BLOCK`/`VirtioBlock` 四个串，
  **全部为 0**（对照：`Libkern`=5、`IOMedia`=28、`apfs_module_start`=1，证明检索方式有效），
  所以"换一个已经构建好的内核试试"这条路在本机不存在。**此处 2026-10-25 更正过一次**：早先写的是
  "四个"，实际去重是五个——少数一个不会改变结论，但一个数错的计数会让人以为清点做过了。
- **launchd PID 1 因此从未被观测到。** §1.4 证明的是交接与空闲线程，不是用户态。

**结论：根卷阻断是「需要一个本机不存在的内核配置及其整条 owner 签名链」，
不是「需要一个本机不存在的镜像」。** 早先"缺的不是镜像而是挂上去"的说法是错的，已在此更正。

**而这个内核配置为什么拿不到，位置在 runner 而不在本机**：runner 的 M2 计划里
`build-os-root` 就排在 `build-kernel` 前面，而 `build-kernel` 只在 `root_chain_present`
为真时才换成 `qemu_root_defconfig`；可是 `build-os-root` 要一个**本地** IPSW，workflow
只调用一次 `build-macos.sh`、从不传 `--ipsw`，它自己的 IPSW 步骤取完 kernelcache 就把
6.8 GB 镜像删掉了。所以 runner 上 `build-os-root` 恒 `not_run`、无链、内核恒
`qemu_defconfig`。详见 `docs/implementation-plan.md` 事实 45。

---

## 7. 统一启动入口：`python -m unipad.host.unixnu_boot`（2026-10-27）

§6 的探针是**量具**，不是启动路径。它回答一个封闭问题（这个内核能不能枚举第二个块设备），
顺带做了三件启动路径不该做的事：把 UDIF 容器解开、按 `board.toml [ipsw.sha256]` 给镜像点名、
在 QEMU 运行期间反复读活访客。启动路径要的是同一份**配置**，不要那把尺子。
本节记的就是这两边的边界，以及统一入口现在能表达的全部配置项。

```
python -m unipad.host.unixnu_boot \
  --device vendor/UniXNU-qemu \
  --qemu   local-private/tools/qemu/qemu-system-aarch64.exe \
  --firmware local-private/tools/qemu/share/edk2-aarch64-code.fd \
  --kernel <kernel.macho> --devicetree <kernel.macho.adt> \
  --darwinboot <DarwinBoot.efi> --unixnu <UniXNU.efi> \
  --workdir <run dir>
```

七个 `--device/--qemu/--firmware/--kernel/--devicetree/--darwinboot/--unixnu` 是必填
（一个默认值就会造出没人清理的运行目录），其余全是可选项，而**每一个可选项都对应一样
诊断脚本验证过、曾经只有诊断脚本能表达的东西**：

| 旗标 | 挂上什么 | 缺了会怎样 |
| :--- | :--- | :--- |
| `--payload-toml` | 采用一份现成的 `unixnu.toml` 而不是按板级 spec 现生成 | 生成的那份；runner 的 payload 构建写的那份参数更准 |
| `--trust-cache` | 拷成 `EFI/UniXNU/Payloads/kernel.macho.trustcache` | AMFI 拒绝 exec launchd（namespace 9 / EXC / code 8 / SECURITY_POLICY，`build-os-root.py:26-27` 自己写着） |
| `--data-ramdisk` | 校验后按 `DATA_RAMDISK_NAME` 拷上，并往 `unixnu.toml` 写 `initrd` 行 | DarwinBoot 以 `EFI_COMPROMISED_DATA` 在内核启动前拒绝（`DarwinBoot.c:563-569`） |
| `--root-image` | 第二个只读 virtio-blk 块设备 | 客体只有 ESP，堵在 `vfs_mountroot` |
| `--systemos-image` | 第三个只读 virtio-blk 块设备 | `native_owner_cache_prepare()` 无可挂之物，panic `offline owner SystemOS mount failed: 80 (EAUTH)`（`kern_exec.c:8032`） |
| `--kernel-arg NAME=VALUE` | 替换板级同名参数，可重复 | 用板级自己的 `kernel_args` |
| `--attempts N` | 每个 attempt 一个全新 `attempt-N/` 目录，见到 Data 挂载就停 | 启动一次（`boot()` 本身的行为） |
| `--ramfb-driver` | 校验存在后按 `RAMFB_NAME` 拷上，`build_argv` 随之发出 `ramfb-size` fw_cfg 项 | 不拷：`ramfb_size` 恒为 `None`，画面停在 QEMU 的 800x600 默认值（§7.4 实测） |
| `--display/--qmp-port/--seconds/--every` | 窗口、QMP TCP 端口、观测时长与间隔 | `none` / 默认端口 / 模块默认值 |

`--trust-cache` 是 2026-10-27 补的，也是最后一项。补它之前出现过一个很难看的事实：
`BootInputs.trust_cache` 有字段、`missing()` 查它、`to_dict()` 记它、`stage_esp()` 按
`TRUSTCACHE_NAME` 拷它、`_check_carried()` 护它——**只有 CLI 的 argparse 没有这一项**。
于是诊断脚本验证过的那份配置只能从诊断脚本到达。`--data-ramdisk` 与 `--systemos-image`
当初是因为「正式路径在丢它们」才被补上的，`--trust-cache` 没有那样的触发事件，所以漏到现在。
**「收敛」的实操含义因此不是把诊断的代码复制进正式路径，而是正式路径能表达诊断验证过的
每一个配置项；差一项就不叫收敛。**

`--trust-cache` 与 `--data-ramdisk` 是**一对**，不是两个独立开关：DarwinBoot 要求
`Request->Ramdisk != NULL` 时 `/chosen/memory-map` 必须带一个 16 字节**全零**的
`PackedRAMDisk`（或 `RAMDisk`）属性，`Request->Ramdisk == NULL` 时又要求树上**没有**它。
`stage_esp()` 查这一对而不是信调用方两边都摆对了；设备树那一半由
`unipad.host.adt --data-ramdisk` 写。

### 7.1 运行输出会说这是哪一种运行

`main()` 对每一个可选项都打印带没带，读的是 `inputs` 而不是 `report`：
`BootReport` 故意不带 trust cache 字段——一份无条件声称带了 trust cache 的报告，
分不出带过与没带过的运行，而这个差别在串口日志里看不见。同理
`inputs.to_dict()` 在没有该项时**省略这个键**。断言这些输出时要用整行：
`"no trust cache attached"` 包含 `"trust cache attached"`，子串形式会在它本该排除的
分支上通过（这个错误在本文件与 `tests/unit/test_host_unixnu_boot.py` 里各犯过一次，
`docs/implementation-plan.md` 事实 44 记着第三次）。

### 7.2 探针做而统一入口不做的两件事，以及为什么

1. **UDIF 解码。** `--root-image` 收到容器**直接拒**（`config_invalid`），不解开。
   QEMU 的 `format=raw` 读不了 UDIF，而解开要一个 LZFSE 解码器；统一路径选择指名拒绝
   而不是近似，探针选择解开因为它要量的就是那个镜像。要喂容器，先用探针或
   `unipad.host.udif` 解开。
2. **按板级摘要点名。** 探针把镜像的 sha256 对回 `board.toml [ipsw.sha256]` 并报出角色，
   挂成 SystemOS 时告警。统一入口只验「可用」：不存在、为空、是 UDIF 容器是三种不同的拒绝，
   但不判断它是哪个成员。材料身份属于交付与清单（`unipad.host.material_manifest`），
   不属于启动 argv。

观测循环**原来**也在这份名单上，2026-10-27 之后不在：见 §7.3。

### 7.3 统一入口现在也读活访客了：一次 1/8 的竞态

§7.2 第 3 条原来的说法是「`boot()` 自己也截图、读串口、等 QMP `query-status`，但**不反复挖
客体内存**——那是量具的职责」。这句话现在是错的，而且是被一次实测逼错的。

**现象。** 同一套材料连启 8 次，7 次挂上 Data 卷并继续启动，1 次在内核里 panic。
失败率 **1/8**（n=8，样本很小，只够定位机制、不够当概率用）。

**机制。** `selectVolumeSeed`（`NativeRamBlock.cpp:368-417`）只发**一次**
`IOService::getMatchingServices` 找 Data 卷的 `IOMedia`，`:414` 是
`if (!selected || matches != 1) return 0;`——**没有重试、没有 sleep、没有任何等待**。
`load_init_program` 在这个 0 上 panic。客体输掉这次竞态就是死了，而宿主看不见。

**为什么看不见。** XNU 的 panic 路径把 CPU 停在 `wfe; b wfe`，**QEMU 因此不会退出**。
实测：panic 之后 `query-status` 照样回 `running`、截图照样成功、`boot-report.json` 的
其它每一个字段都像一次健康运行。能区分的只有客体内核日志——还在启动的客机会继续往
`msgbuf` 写，panic 了的永远不会再写。

**因此主机侧做重试。** 内核不能改（那是上游），竞态发生在客体里，但**重试可以发生在主机里**：

| 位置 | 职责 |
| :--- | :--- |
| `_guest_log_probe()` | 每一轮观测读一次 msgbuf 环，返回字节数、原始字节的 sha256、有没有见到挂载标记 |
| `guest_stopped_progressing()` | 判据：**日志停止写入且始终没有挂载标记** |
| `boot_until_mounted()` | 换一个全新目录再启一次，见到挂载就停 |

判据只看「日志还在不在长」，**不看日志内容**。这个构建里那两条 panic 字符串带一个
`@%s:%d` 后缀，在 vendored 树里找不到来源（任务 #29），按 panic 文本匹配就是在匹配一个
本工程还解释不了的字符串，后缀一改判据就失效。指纹只对**原始字节**取，落盘的那份要过
`scrub_account()`——否则指纹会随主机账户名变化，而它是要跨机器可比的。

`--attempts` 的 **CLI 默认是 1**，也就是「启一次」——`boot()` 本来的行为，命令行不因为多了一个
开关就改变语义。**库函数 `boot_until_mounted()` 的默认是 3**（`MAX_BOOT_ATTEMPTS`），因为调它的
代码已经决定要重试了。这两个默认值故意不同，别把它们对齐：1/8 的三次方是 1/512，低于本工程测过
的任何东西，同时又小到真出现回归时调用方几分钟内就知道，而不是永远不知道。
**每个 attempt 一个全新目录**（`attempt-N/`）：`boot()` 会用 firmware 自己的大小截断出 `vars.fd`，
两次启动写进同一个目录就是两台不同的机器，两份看起来可比的报告描述的却不是同一台。

探针与截图**串行**，不并发：QEMU 的 QMP 一次只服务一个客户端。`_watch()` 每轮先截图、再探一次，
顺序由 `test_the_probe_runs_after_the_screendump_in_every_round` 钉住。但**截图失败不能连带杀掉探针**：
截图原来是内联在 `_watch()` 里的，带三个 `continue`，每一个都把探针一起跳掉了——于是一次截图失败
（QMP 拒绝、或 `_write_png` 解不了那个 PPM）会让整轮没有 `guest_probes`，`data_volume_mounted` 恒为
`False`，`boot_until_mounted()` 把三次预算全花在一个第一次就挂好了的客体上。这个形态最难看的地方在于
**它在好路径上完全隐形**：实测的启动里截图从来没失败过，所以这个耦合从不触发，而本该消掉 12.5% 的重试
会在一个不相关子系统坏掉时变成 100% 失败率。现已抽成 `_take_screenshot()`，三种失败都只记录
`screendump_error` 并返回 `None`，由两个回归测试钉住。读一页 4 KiB 要走 `pmemsave` 到暂存文件
再读回，一次探针是一整个来回；实测环约 128 KiB，即一次探针约 32 个来回，16 MiB 的上界
（4,096 个来回）是 `guestobserve` 的拒绝线而不是常态。

报告新增：`data_volume_mounted` / `data_marker` / `guest_log_bytes` /
`guest_log_fingerprint` / `guest_probes[]` / `attempts` / `attempt`。
`attempts` 是这条链启了几次，`attempt` 是**这份报告来自第几次**——两个字段故意分开：
前者是调用方收到的报告的性质，后者是它被写进哪个目录的性质。
**只有最后那一个 attempt 的 `boot-report.json` 带真实的链长**：早先 attempt 的文件里
`attempts` 是 1，因为那是写下它时所知道的事，事后改写就是伪造那次 attempt 实际报告的内容。
所以那些文件要当作按 `attempt` 编号的单次记录读，返回的对象才是一条链的裁决。

**状态：代码完成、单测与变异验证通过、未在真实启动上验收。** 重试本身要生效需要一次
真实的 1/8 失败，而那正是它要消掉的东西。

### 7.4 `--ramfb-driver`：把一条死检查接上，以及它**没有**修好画面（2026-10-28）

起因是一个不可能为真的判断。`build_argv()` 只在 ESP 上真有
`EFI/UniXNU/Payloads/QemuRamfb.efi` 时才发
`-fw_cfg name=opt/org.unixnu/ramfb-size,string=1376x1024`，`boot()` 也用同一个条件决定
`ramfb_size`——**但本模块没有任何代码能把那个文件放上去**。`stage_esp()` 甚至明确拒绝 staging
它，理由是会形成 `SCREEN` 的第二处定义。于是官方路径上这个判断恒为假，`ramfb_size` 是死代码；
`--trust-cache` 那次的形状（正式路径表达不了诊断验证过的配置）在这里重演了一遍。

现在 `BootInputs` 多一个可选 `ramfb_driver`，`stage_esp()` 按 `RAMFB_NAME` 拷它，
CLI 多 `--ramfb-driver`。几何仍然只在 `SCREEN` 一处定义：驱动只**接收**这个值，不定义它。
`RAMFB_NAME` 是 staging 和判断共用的唯一拼写，由
`test_the_geometry_is_defined_in_exactly_one_place` 数源码里带引号的字面量钉住——
那个测试是在第一次变异审查发现「两处拼写」的变种活下来之后补的。

**然后量了，结论是它没有修好画面——而原因比假设更靠前一格。** 用本机已有的那份驱动
（12,800 字节，sha256 `8aa46db3ac008aaa72aab10cc3efd97d5be29855aad00b1d7bd590941386d85c`，
与事实 14 的记录一致，启动前逐字节校验）做了一次 24 分钟、每 45 秒一次 screendump 的启动：

| | 不 staging 驱动（对照） | staging 驱动 |
| :-- | :-- | :-- |
| screendump 几何 | 800 600 | 800 600 |
| screendump 大小 | 1,440,015 字节 | 1,440,015 字节 |
| sha256 | `549bfa61991c` | `cc031c4ebefe` |
| dump 次数 / 去重数 | 13 次 / 1 种 | 29 次 / 1 种 |

**第一版读法，是错的，记在这里免得再错一次**：当时把 sha256 的变化读成「驱动确实被加载并
运行了，它改了画面内容，只是没改几何」。**这个推论不成立。** 真实原因是：
交付的 `unixnu.toml` 只列了一个 driver——`Payloads/DarwinBoot.efi`。
UniXNU 加载**配置点名**的驱动，固件不会去枚举 `Payloads/` 目录；
所以那个文件在 ESP 上，却没有任何东西会打开它。几何没变是必然的，
sha256 的变化与驱动无关（最可能是 ESP 的 FAT 目录多了一项，改变了 UniXNU 菜单画的东西）。

**所以缺的不只是 staging，还有配置里那一行。** `unixnu_toml()` 现在多一个 `ramfb` 参数，
在 **DarwinBoot 之前**写一条 `[[driver]]`——顺序不是排版选择：
`QemuRamfbPkg/QemuRamfb.c:1-7` 明说这个驱动的存在意义就是「在 DarwinBoot 取走帧缓冲之前」
定好 ramfb 的尺寸，列在它后面就是去配置一个已经被认领的帧缓冲。
由 `test_the_generated_config_loads_the_driver_before_darwinboot` 钉住顺序。

**同时加了一条拒绝，因为「摆了个没人加载的文件」必须响一声。** `_check_ramfb_pair()` 在
调用方**采用**（`payload_toml`）一份不点名该驱动的配置时直接拒绝。不注入那一行是刻意的：
启动配置必须来自匹配的构建（目标原文：「启动配置必须来自匹配的构建」），
改一份匹配构建交下来的配置就是伪造配置。缺的是 runner 侧 payload 构建的一行
`[[driver]]`，那是构建改动，不是本模块该冒充的东西。

**画面不更新与几何是两件事，而且前者才是阻塞项。** 29 次 dump 全同、跨 1,305 秒，
和对照组的 13 次全同一样。画面有真实内容（23 种颜色，iOS 蓝、灰、DarwinBoot 的绿条，
600 行里 342 行不同），说明开机阶段画过，之后**再也没有人画过第二帧**。
§5.2 的第 2 条端到端用例要求「进入真实 SpringBoard，通过鼠标操作改变客体界面」，
其机器化判据直接是 `framebuffer_before != framebuffer_after`——所以阻塞项是
**没有第二帧**，不是分辨率。而 PRD:202 明确「输入必须先于交互验收完成」，
所以下一步是输入链路，不是继续追分辨率。

**状态：代码完成、单测与变异验证通过；上述启动测量为真实 Windows 验收，结论为「未修复」。**
`--ramfb-driver` 本身照常可用——它把一条死检查接上了，但那不是画面不更新的解。

---

### 7.5 `--serial-port`：为什么从来没有一个按键到达过客体（2026-10-28）

这一节修的是一个**链路本身**的缺陷，不是某个参数没配对。

#### 7.5.1 结论：`-serial file:` 是单向的

`build_argv` 过去只发 `-serial file:<run>/serial.log`。QEMU 对这一种的语义是：
**客体往文件里写，主机写进去的一切被丢弃。** 所以「给客体发按键」这件事在这个配置下
不是难，是**不可能**——与按键编码、坐标换算、QMP `send-key` 全都无关。

#### 7.5.2 客体那边确实在听，而且听了很久

这不是推测，是日志里有的。某次运行的串口日志在 **t=43.75s** 打印
`Serial keyboard started`，而 XNU 的 `serial_keyboard_init()`
（`osfsk/console/serial_general.c:51-68`）**只在** `serialmode & SERIALMODE_INPUT`
时才打印这一行——`serial_protos.h:52,55` 里 `SERIALMODE_INPUT = 0x02`，
本板 `serial=3` 正是 `SERIALMODE_OUTPUT|SERIALMODE_INPUT`。它启动的那个线程每 16 ms
调一次 `_serial_getc(false)`，拿到的字节交给 `cons_cinput()`
（`serial_general.c:79-97`），后者喂给 `km_tty[0]` 的行规程
（`bsd/dev/arm/km.c:397-407`）。

**这些符号不是从某份源码树引来的，是在被测内核二进制里数出来的**：
`local-private/boot32/bundle/unixnu-out/qemu/kernel/kernel.macho`（125,431,808 字节，
sha256 `e6480f661dfa4efd…`）里 `Serial keyboard started` 出现 1 次、
`serial_keyboard_init` 3 次、`cons_cinput` 2 次、`_serial_getc` 4 次、`km_tty` 1 次——
下面引的 `serial_general.c` / `km.c` 行号来自上一轮的源码查证，本地当前没有检出 XNU 树，
所以**行号未经本轮复核**，符号的存在性已经复核。

**所以客体在听一个主机写不进去的端口。** 这也解释了另一件事：这条链路是**内核 console tty**，
不是 IOHIDFamily。字节落在 `km_tty[0]`，任何能打开 `/dev/console` 的进程都读得到——
这是字符通道，不是指针/手势通道。

#### 7.5.3 改法：同一个 UART，两种消费者

`_serial_argv()` 现在在给了 `--serial-port` 时改发：

```text
-chardev socket,id=unixnu-serial,host=127.0.0.1,port=14533,server=on,wait=off,logfile=<run>/serial.log,logappend=on
-serial  chardev:unixnu-serial
```

`logfile=` + `logappend=on` 是关键：**QEMU 自己仍然写 `serial.log`**，
所以观察链路一个字节都没变——这恰恰是单向性一直没被发现的原因：
两种配置产出的 `serial.log` 完全一样，从运行输出里看不出任何差别。
不给 `--serial-port` 时行为与从前逐字节一致（`test_no_serial_port_keeps_the_file_spelling_it_always_had`、
`test_boot_without_a_serial_port_writes_the_file_spelling` 两头钉住）。

`main()` 现在会把这一项**打进运行输出**，理由和几何那一行相同：
一个只写的 UART 和一个双向的 UART 产出同一份 `serial.log`，
不说一声的话，事后没有任何办法从运行自己的输出里分辨是哪一种。

#### 7.5.4 `SerialInputSink`：能做的一半，和拒绝的另一半

`src/unipad/host/input.py` 新增 `SerialInputSink`，实现 `InputSink` 接口：

| 方法 | 行为 | 为什么 |
| :--- | :--- | :--- |
| `text(s)` | 逐字符 `sendall` UTF-8 | 客体 `_serial_getc` 拿到的就是这些字节 |
| `key("q")` | 等于 `text("q")` | 单字符就是它自己 |
| `key(["ctrl","a"])` / `key("f1")` | **拒绝** | tty 行规程收字节不收键码；命名的键没有字符可发 |
| `tap` / `drag` / `home` / `pointer_move` | **拒绝**（消息里点名 `km_tty`） | 字节进 `cons_cinput`，没有 `IOHIDDevice` 消费它 |

**指针类方法拒绝而不是近似，这是刻意的。** 一个「收到 tap 就发个空格」的 sink 会让
`input_journal` 记下一个从没发生过的手势——这正是 `InputSink` 契约要防的失败形状
（丢掉的手势看起来像客体没响应，伪造的手势看起来像它能用）。

#### 7.5.6 两条独立的主机→客体输入路径，不要混

这一节加上去是因为「输入」在这个工程里其实是**两条路**，而十条 E2E 的第 2 条只吃其中一条：

| 路径 | 主机侧 | 客体侧落点 | 能否改变帧缓冲 |
| :--- | :--- | :--- | :--- |
| **控制台字符** | `SerialInputSink` + `--serial-port` | `km_tty[0]` 行规程，经 `cons_cinput` | **不能** |
| **USB HID** | `QmpInputSink.send_key()` / `input_send_event()` | `usb-kbd`（挂在 `qemu-xhci` 上） | **可能** |

`build_argv` **一直就带着** `-device qemu-xhci` + `-device usb-kbd`（`unixnu_boot.py:1270-1271`），
而 `QmpInputSink`（`input.py`）经 `QmpClient.send_key()`（`qmp.py:613`）发 QMP `send-key`。
所以 §5.2 第 2 条端到端用例的机器判据 `framebuffer_before != framebuffer_after`
**不该由串口这条路去满足**——串口是字符通道，SpringBoard 不读控制台字节。

**一条诚实的未验证项**：`usb-kbd` 在 argv 里、`send-key` 有实现，但**客体那端的 IOHID 栈
是否消费这个 USB 键盘，一次都没量过**。`-display none` 之下 QMP `send-key` 注入到哪个
console 也没有量过。这两件事是 E2E 第 2 条的前置，记在这里而不是当成已有结论。

#### 7.5.8 USB HID 路实测：客体要的是 **VirtIO** 设备，而这条路已经**通到驱动层**（2026-10-28）

先纠错：上一版这里写的是「`usb-kbd` 在 argv 里、客体是否消费它未量过」。**这个问法本身
就是错的**，因为 `usb-kbd` 根本不是客体能看到的设备。

**实测：`kernel.macho.adt` 里 `usb`/`xhci`/`dwc3`/`hid` 各出现 0 次，`virtio-mmio` 出现
32 次。** 所以这块板上没有 USB 控制器节点，xHCI 驱动无处可绑；而 32 个 VirtIO MMIO 节点
正是 `package/VirtioInput.cpp` 用的传输层。上游自己的 `run-qemu.py:208-210` 也只在这条
`--os-root` 路径上发一对 **`virtio-tablet-device` + `virtio-keyboard-device`**，注释逐字
点名消费者：

> The pointer and keyboard of the QEMU window for `package/VirtioInput.cpp`: a click and
> drag is a touch, Home or F1 is the Home button. QEMU sends its input to them once the
> driver has started them.

**所以这是第三个同形状的缺口**（前两个是 `--trust-cache` 和 `--ramfb-driver`）：
诊断脚本验证过的配置只有诊断脚本发得出来，统一入口发的是 `qemu-xhci` + `usb-kbd`——
一个客体没有驱动的键盘。`build_argv` 现在补上这一对（同样只在有 `--root-image` 时发，
理由与上游一致：没有 OS root 就没有用户态去消费触摸，而一个客体枚举了却没人驱动的设备
是两次看起来一样的运行之间一个无声的差异）。

**补上之后的实测，用的是客体自己公布的计数器**（`local-private/tmp/virtio-counters.py`）：

| 计数器（`VirtioInput.cpp` 的 `volatile uint32_t` 全局） | 发送前 | `send-key home` 后 |
| :--- | :--- | :--- |
| `native_virtio_input_devices` | **2** | 2 |
| `native_virtio_input_events` | 0 | **4** |
| `native_virtio_input_keys` | 0 | **2** |
| `native_virtio_input_touches` | 0 | 0 |
| `native_virtio_input_failures` | 0 | 0 |

**结论：QMP `send-key` → QEMU `virtio-keyboard-device` → 客体 `VirtioInput.cpp` 这条路
已经实测打通**，客体自己数到了两次按键。这条不是从帧缓冲推断的，是从客体内核全局量出来的
（做法同事实 103 的持久化状态：按符号名读活客体，而不是 grep 一个早就绕圈的环形缓冲）。

**顺带修掉一个会让人反复发错键的坑**：先发的是 `ctrl-alt-del`，`events` 从 0 涨到 12 而
`keys` 恒 0——因为 `VirtioInput.cpp:30` 只把 **Home（evdev 102）和 F1（59）** 当成按键，
其余键只进 `events`。上游 `qemu-input.py` 的说明也是「Home or F1 is the Home button」。
**发错键时的现象（有事件、无按键、帧缓冲不变）和链路不通时的现象一模一样**，这是这条路上
最容易得出错误结论的地方。

**仍未解决的是另一件事，与输入无关**：帧缓冲哈希在按键前后逐字节相同
（`c2cfec2e70fc`，1376x1024）。客体数到了按键，但**没有第二帧**。
所以 E2E 第 2 条的判据 `framebuffer_before != framebuffer_after` 目前卡在
「没人画第二帧」，不再卡在「输入进不去」。`native_hid_input_command` 是否被
`NativeHidInput` 取走、SpringBoard 是否因此动作，尚未测量。
**仍未解决的是另一件事，与输入无关**：帧缓冲哈希在按键前后逐字节相同
（`c2cfec2e70fc`，1376x1024）。客体数到了按键，但**没有第二帧**。
所以 E2E 第 2 条的判据 `framebuffer_before != framebuffer_after` 目前卡在
「没人画第二帧」，不再卡在「输入进不去」。`native_hid_input_command` 是否被
`NativeHidInput` 取走、SpringBoard 是否因此动作，尚未测量。

#### 7.5.9 「没有第二帧」的根因定位到客体自己的一行代码（2026-10-28）

先纠正 §7.5.8 结尾的说法：当时写「卡在没人画第二帧」，那只是现象。
**根因已经量出来了，而且是客体驱动自己公布的计数器说的**，
诊断脚本 `local-private/tmp/legacy-gate.py`（沿用事实 103 的做法：按符号名读活客体内核全局）。

`package/LegacyDisplayProbe.cpp` 的 `native_legacy_display_late_probe()` 在创建
GOP nub **之前**有一道门（`:490-494`）：

```cpp
native_legacy_surface_status = OSKextLoadKextWithIdentifier("com.apple.iokit.IOSurface");
OSDictionary *matching = IOService::nameMatching("IOCoreSurfaceRoot");
IOService *surface = matching ? IOService::waitForMatchingService(matching, 5000000000ULL) : nullptr;
native_legacy_surface_found = surface != nullptr;          // <-- 这一条
PE_Video video = {};
if(!native_legacy_surface_status && native_legacy_surface_found && !PE_current_console(&video)
    && video.v_baseAddr && ... && video.v_depth == 32 ...) {
    native_legacy_video_base = video.v_baseAddr;           // <-- 从来没跑到这里
    ...创建 IOPlatformDevice nub、attach、start、挂到 /arm-io ...
}
```

**实测这一道门死在哪一条，已经分离出来了**：

| 计数器 | 值 | 含义 |
| :--- | :--- | :--- |
| `native_legacy_surface_status` | **0** | IOSurface kext **加载成功** |
| `native_legacy_surface_found` | **0** | **`IOCoreSurfaceRoot` 在 5 秒内从未出现** |
| `native_legacy_video_base` | 0 | 门没过，从未记录帧缓冲地址 |
| `native_legacy_nub` | 0 | 门没过，从未创建 nub |
| `native_legacy_dt_attached` | 0 | 从未挂到设备树 |
| `native_legacy_probe_match` | 0 | 显示驱动从未 probe 成功 |
| `native_legacy_start_result` | 0 | 显示驱动从未 start |
| `native_gop_ready` / `native_gop_bytes` | 0 / 0 | presenter 从未就绪，从未 present |
| `native_legacy_frame_count` | 0 | 从未有帧 |

**所以链条是**：`IOCoreSurfaceRoot` 不出现 → `native_legacy_surface_found = 0` →
早退，`native_legacy_video_base` 保持 0、nub 从未创建 → `native_gop_ready = 0` →
没有任何东西 present → **帧缓冲永远是 DarwinBoot 留下的那一帧**。
这与「输入进不去」完全无关，输入那条路已经打通（§7.5.8）。

**两个必须一起说的边界，否则会得出过头结论**：

1. **帧缓冲地址其实是到了内核的。** 客体日志 t=12.8s 有
   `initialize_screen: b=1BBEF0000, w=00000560, h=00000400, r=00001580, d=00000000`
   （1376x1024，行 5504），而且**没有**打印 `No video - forcing serial mode`。
   所以 §7.5.7 里「内核从未被告知帧缓存在哪」那句**只对 `native_legacy_video_base`
   这个变量成立，对 `PE_state.video` 不成立**——前者是这道门没过所以没赋值。
2. **`d=00000000` 与 DarwinBoot 的 `depth=32` 不一致**，而驱动两处都要求
   `video.v_depth == 32`。第一个探针的门（`:456`）显然过了（对象分配成功、
   `init_result=1`、`parent_match=1`），所以**至少在那个时刻 depth 是 32**；
   但 `initialize_screen` 打的是 0。两者谁在反映同一个 `PE_state.video` 未经确认，
   **不要据此断言 depth 一定是问题**。

**下一步就一件事**：查 `IOCoreSurfaceRoot` 为什么从不出现。两个候选，
代价不同也不要混：(a) IOSurface kext 加载了但没 start（`OSKextLoadKextWithIdentifier`
只加载不启动，而 `IOCoreSurfaceRoot` 是它的 `IOService` 发布物）；(b) 启动得太晚，
5 秒窗口（`waitForMatchingService(..., 5e9)`）不够。判别式：把窗口内
`IOService::waitForMatchingService` 的等待拉到更久，或在客体侧确认 IOSurface 是否 start。
**下一步就一件事**：查 `IOCoreSurfaceRoot` 为什么从不出现。两个候选，
代价不同也不要混：(a) IOSurface kext 加载了但没 start（`OSKextLoadKextWithIdentifier`
只加载不启动，而 `IOCoreSurfaceRoot` 是它的 `IOService` 发布物）；(b) 启动得太晚，
5 秒窗口（`waitForMatchingService(..., 5e9)`）不够。判别式：把窗口内
`IOService::waitForMatchingService` 的等待拉到更久，或在客体侧确认 IOSurface 是否 start。

#### 7.5.10 判别式跑完了：服务**会**出现，只是晚于探针的 5 秒窗口，而且**没有重试**（2026-10-28）

上一节的两个候选，实测答案是 **(b)，而且比 (b) 更严重一层**。

一次专门等 `IOSurfaceRoot::installMemoryRegions()` 出现再读计数器的运行
（`local-private/boot33/surface-race/`，诊断脚本 `local-private/tmp/surface-race.py`）：

| 量 | 值 |
| :--- | :--- |
| `IOSurfaceRoot::installMemoryRegions()` 首次出现 | 客体 **t=86.1s** |
| `native_legacy_surface_found`（服务已经存在之后读） | **0** |
| `native_legacy_surface_status` | 0（kext 加载成功） |
| `native_gop_ready` | **0** |
| `native_legacy_nub` / `native_legacy_video_base` | 0 / 0 |
| `native_legacy_frame_count` | **0** |

**所以「`IOCoreSurfaceRoot` 从不出现」这个说法是错的，要改**：它出现，只是晚于
探针那一次 5 秒 `waitForMatchingService`。而 `native_legacy_surface_found` 是
**一次性赋值、没有任何重试**的，所以服务后来出现了也没用——`native_gop_ready`
在服务存在之后仍然是 0，`native_legacy_frame_count` 仍然是 0。

**这就是「没有第二帧」的完整根因，一句话**：
`native_legacy_display_late_probe()` 的 5 秒等待窗早于 `IOSurfaceRoot` 安装
（本次 t=86.1s，observe2 那次 t=51.8s——**两次差 34 秒，说明这个先后不是固定的**），
窗一关 `surface_found` 就永久为 0，GOP nub 永不创建，`native_gop_ready` 永为 0，
没有任何东西 present，帧缓冲永远是 DarwinBoot 留下的那一帧。

**修法在客体侧，不在主机侧**：`package/LegacyDisplayProbe.cpp:490-494` 要么把等待拉长、
要么在超时后挂一个通知重试（`IOService::publishNotification` / `addMatchingNotification`），
这是 `vendor/UniXNU-qemu` 树的改动，要重新构建内核并经 runner 交付——
**不是 `src/unipad/` 能修的，也不要试图用主机侧的启动参数绕过**。
主机侧唯一能做的是把它记录成阻塞项，并保证一旦内核修好，
现有链路（`--ramfb-driver`、VirtIO HID、`send-key`）不需要再改一次。

**一条附带的实测**：`IOSurfaceRoot` 的安装时刻在两次运行之间差了 34 秒
（51.8s vs 86.1s），所以「探针跑在服务安装之前」不是一次偶然的竞态，
而是一个**时窗不稳定、但探针只试一次**的结构性组合。
**一条附带的实测**：`IOSurfaceRoot` 的安装时刻在两次运行之间差了 34 秒
（51.8s vs 86.1s），所以「探针跑在服务安装之前」不是一次偶然的竞态，
而是一个**时窗不稳定、但探针只试一次**的结构性组合。

#### 7.5.11 一条必须记下来的**不可分辨**：现有计数器分不开两种早退（2026-10-28）

上面把根因归到「5 秒窗早于 `IOSurfaceRoot` 安装」。**这个归因比证据强一步，
在这里收回一半。**

`native_legacy_display_late_probe()` 有**两道**可能早退的地方：

```cpp
IOService *display = pendingLegacyDisplay;
if(!display) return;                                    // (A) 第一个探针把它 release 了
pendingLegacyDisplay = nullptr;
native_legacy_surface_status = OSKextLoadKextWithIdentifier("com.apple.iokit.IOSurface");
native_legacy_surface_found  = (waitForMatchingService(nameMatching("IOCoreSurfaceRoot"), 5e9) != nullptr);
if(!status && found && !PE_current_console(&video) && video.v_depth == 32 && ...) {   // (B)
    native_legacy_video_base = video.v_baseAddr; ...创建 nub、attach、start...
}
```

**(A) 和 (B) 在现有计数器下产出完全相同的读数**，因为
`pendingLegacyDisplay` 是 `static IOService *`——**文件内链接，没有符号**，
读不到；而 `native_legacy_surface_status`/`_found` 的 .bss 初值本来就是 0，
「赋值成 0」和「从未赋值」分不开。

**能确定的是这些**（`local-private/boot33/legacy-gate/counters-before.json`）：
显示驱动**一件事都没做**——`native_legacy_registration_calls = 0`、
`native_legacy_copy_n = 0`、`native_legacy_copy_bytes_sum = 0`、
`native_legacy_presented_generation = 0`、`native_legacy_retired_frames = 0`、
`native_legacy_frame_count = 0`；`native_legacy_nub = 0`、`dt_attached = 0`、
`probe_match = 0`、`start_result = 0`、`native_gop_ready = 0`。
**第一个探针的门（`:456`）显然过了**，因为对象分配成功
（`native_legacy_object = 0x3E52_xxxx`）、`init_result = 1`、`parent_match = 1`——
而那一道门同样要求 `video.v_depth == 32`。

**所以诚然的结论是**：显示驱动在 `(A)` 或 `(B)` 之一停下，
**现有仪器无法判定是哪一道**；`IOSurfaceRoot` 确实晚于 5 秒窗安装（t=51.8s / 86.1s）
支持 `(B)`，但不排除 `(A)`——若 `PE_parse_boot_argn("offline-legacy-phase")`
没把 ASCII `4` 解析成 4，第一个探针就会走 release 分支，
而 `offline-legacy-phase=4` 确实在生成的 `unixnu.toml` 里（已核对）。

**要分开它，需要的是一次带仪器的客体构建**（把 `pendingLegacyDisplay`
或两道门各 publish 一个计数器），不是再读一遍同样的全局。
**两条修法都在客体侧**：`(B)` → 拉长等待或挂 `addMatchingNotification` 重试；
`(A)` → 查 `PE_parse_boot_argn` 对 `unsigned` 的 ASCII 解析，或把 phase 改成显式传递。
在分开之前，**不要只按其中一条下手**。

#### 7.5.7 串口最后一跳实测：**无法从客体外判定**，而不是失败（2026-10-28）

一次真实 Windows 运行（`local-private/boot33/serial-input/`，kernel sha256
`e6480f661dfa4efd…`，诊断脚本 `local-private/tmp/serial-input-check.py`）：

| 观测 | 结果 |
| :--- | :--- |
| chardev 进入 argv | **是**，`socket,…,port=14533,…,logfile=serial.log,logappend=on` |
| 客体串口键盘线程 | **在跑**，客体 t=48.985174s 打印 `Serial keyboard started` |
| 主机写入 | **成功**，`SerialInputSink.text("UNIPAD-SERIAL-OK")` 返回 `delivered: True`，16 字节 |
| 客体日志增长 | 发送后 **+250 字节**，之后 37 秒冻结 |
| 帧缓冲 | **1376x1024**，发送前后哈希相同 |
| marker 回显 | **无** |

**那 +250 字节不是输入造成的，这一点必须说清楚**：它们是客体 t=50.089076 的
`disk5s1 mount-complete volume OwnerData` 与 t=50.15s 的 `load_init_program: attempting
to load /sbin/launchd`——Data 卷挂载与 launchd 启动，是开机进程本身，与 marker 无关。
把这段增长当成「客体对输入有反应」就是又一次读错证据。

**所以这一跳的结论是「无法从客体外判定」，不是「失败」。** 理由是结构性的：
字节即使到达 `cons_cinput`，也只会进 `km_tty[0]` 的输入队列，而**客体上没有任何进程
读 `/dev/console`**（iPadOS 没有 getty/login），行规程也没有可见的回显路径；
内核环形缓冲 `msgbuf` 记的是 `printf`，不是 tty 输出。**「没有回显」证明不了「没到达」。**
反过来也成立：主机侧 `sendall` 成功只证明字节离开了主机，证明不了它们被客体取走。

**要判定它，需要的是一次有消费者的实验**，而不是更好的探针。可行的下一步按代价排序：
(1) 让客体侧有东西读 `/dev/console`（这要求能往客体里放一个程序，即 IPA/文件系统写入）；
(2) 直接读客体内存里 tty 输入队列的字节数（需要符号，脆弱）；
(3) **换一条有可见消费者的路**——即 §7.5.6 的 USB HID 路，SpringBoard 就是消费者，
帧缓冲变化就是可见证据。第 (3) 条同时也是 E2E 第 2 条真正需要的路。

**顺带一个已经被这次运行证明的结果**：帧缓冲几何从 800x600 变成 **1376x1024**
（serial.log 里 DarwinBoot 自己打印 `video base 0x1BBEF0000  1376x1024  row 5504 bytes
32 bpp`）。这是 `--ramfb-driver` + 生成的 `unixnu.toml` + `_check_ramfb_pair` 三者合起来的
第一个正面证据：驱动确实在 DarwinBoot 取走帧缓冲**之前**跑到了。
**但画面内容依旧不变**（发送前后 screendump 哈希逐字节相同），所以「几何修好了」
与「没有第二帧」是两件事，后者仍是 E2E 第 2 条的阻塞项。

**状态**：串口路代码完成、单测与变异通过、Windows 实测量为**无法判定**；
几何修复为**已证实的正面结果**；画面不更新仍为**未修复**。

#### 7.5.5 状态与下一步

- **代码完成**：`SerialInputSink`、`_serial_argv`、`boot`/`boot_until_mounted`/CLI 的 `--serial-port` 贯通。
- **单测与变异验证通过**：串口链路共 17 个测试函数 / 20 个用例
  （`test_host_input.py` 9 个，其中 `pointer_shaped` 参数化 4 例；`test_host_unixnu_boot.py` 8 个）；
  20 个变异体全部被杀（含「记录 journal 早于真正写出」、
  「`close()` 不清 socket」、「tap 改发空格」、「`--serial-port` 不透传到 `boot`/重试链」）。
- **真实 Windows 验收：未做。** 上面所有结论要么是代码层，要么是**既往运行日志**里的
  `Serial keyboard started`；「主机写入的字节真的被 `cons_cinput` 取走」这一跳尚未实测。
  下一步是一次 `--serial-port` 运行 + `SerialInputSink.text()` 发送，然后从客体侧证实收到
  （串口回显，或 `/dev/console` 上可见的输入）。

**四个状态分开记**：代码完成 ≠ 模拟测试通过 ≠ runner 验证 ≠ Windows 真实验收。
本条目前只到达前两个。
