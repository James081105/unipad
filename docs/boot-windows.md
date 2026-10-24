# Windows 本机启动链：实测记录

- 日期：2026-10-24
- 主机：Windows 11，QEMU **11.1.0**（`local-private/tools/qemu/qemu-system-aarch64.exe`，sha256 `952f8975…`），
  固件 `local-private/tools/qemu/share/edk2-aarch64-code.fd`（67,108,864 字节，sha256 `47765fe3…`）
- 内核：`local-private/b5/unixnu-out/qemu/kernel/kernel.macho`，51,859,456 字节，
  sha256 `1f32f09bc7668de0…`，macOS runner run `40081850615` 构建
- 设备树：`local-private/boot6/kernel.macho.adt`，17,624 字节，**本机生成**（见 §3）
- 代码：`src/unipad/host/adt.py`（生成 ADT）、`src/unipad/host/unixnu_boot.py`（搭 ESP 并启动）

本文件的职责是把**已证明的**与**未证明的**分开写。构建阶段名称、Actions 绿色、二进制格式正确，
都不等于启动验收通过。

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

`unipad.host.unixnu_boot.boot()` 实测：TCP QMP（Windows 无 `AF_UNIX`）建连成功；
`serial.log` 落盘；`boot-report.json` 记录完整 argv 与输入清单。

**更正（2026-10-24 晚）**：本节最初写的是"收到 greeting 与 `query-status` 回复"。那句话是**过头了**。
`_qmp` 当时在请求循环里 `return`，于是它在 `qmp_capabilities` 的回复后就返回，**从来没有把调用方给的命令发出去**。
也就是说：`query-status` 没发、`screendump` 没发、`quit` 也没发——每次运行都落到
`process.terminate()`，而报告里记的是一条"没人问过的问题的回复"。

这不是猜测，是 `tests/unit/test_host_unixnu_boot.py::test_the_qmp_round_trip_speaks_the_protocol`
用一条严格 xfail 逼出来的：该测试断言 QMP 交换的形状（greeting → `qmp_capabilities` → 命令），
在修复前必然失败。修复后 marker 按约定移除，测试转为承重测试。

因此 §1.3 现在能证明的只有：**QMP over TCP 能建连、能握手**。截图链路与优雅退出是修复后的
代码路径，**尚未在真实 QEMU 上跑过一次**（见 §4）。这也是"构建阶段绿色不等于启动验收"的一个具体例子：
那段时间测试是全绿的，而 QMP 命令一个都没发出去。

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

改正（已写入仓库，等 runner 验证）：

1. `scripts/macos/build-macos.sh` 的 `step_build_bootloader` 新增两步：
   glob 出 `AARCH64-*/UniXNU.efi`（缺一个 `die`，多一个也 `die`——"两个候选却说不出哪个会启动"
   本身就是证据不完整），然后读 PE subsystem，**不是 10 就记 `failed`**。
   复用 `unipad.host.unixnu_boot.pe_subsystem`，这样 runner 与 Windows 本机不可能对
   "固件会怎么对待这个文件"产生分歧。三种结局都实测过：DarwinBoot.efi → FAIL(11)、
   打补丁的孪生体 → PASS(10)、设备树 → FAIL(不是 PE32+)。
2. `.github/workflows/build-macos.yml` 的 `boot-inputs` 束把 `UniXNU.efi` 列为**第一项**，
   并新增 `manifest.json`：束内每个文件的 sha256 与字节数（`manifest.json` 不哈希自己）。
   理由是实测出来的——三个历史 `kernel.macho` 并不逐字节相同（M2 `9fc119604838856d…`
   vs M4 `1f32f09bc7668de0…），一份不点名自己内核的启动日志是没人能复现的日志。

同时 `unipad.host.unixnu_boot._refuse_non_application()` 在启动前读 PE subsystem，
把上面那条含义不明的固件错误变成一条指名原因的拒绝。

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

---

## 4. 未证明 / 阻塞

| 项 | 状态 | 原因 |
| --- | --- | --- |
| UEFI → UniXNU 交接 | **BLOCKED** | 缺 `UniXNU.efi` 字节（§2），已写好收集改正，等 runner 跑一次 |
| UniXNU → DarwinBoot 交接 | UNVERIFIED | 上一条未过 |
| DarwinBoot → XNU | UNVERIFIED | 上一条未过 |
| 内核进入用户态 / SpringBoard | UNVERIFIED | 上一条未过 |
| QMP `screendump` / `quit` 真的生效 | UNVERIFIED | 代码已修（§1.3），未在真实 QEMU 上跑过 |
| `QemuRamfb.efi` 设置 1376x1024 | UNVERIFIED | 该驱动由 payload 构建放在 ESP 上，本机未生成 |
| 十条端到端验收 | BLOCKED | 同上 |

**没有任何一项被写成"通过"。** 构建阶段名称、Actions 绿色、PE/Mach-O 格式正确，都不代替启动验收。

## 5. 下一条实验假设

把 `UniXNU.efi` 拿到手（runner 新一次 `build-payload` + `build-bootloader` 的产物）后，按
`python -m unipad.host.unixnu_boot --device vendor/UniXNU-qemu --qemu … --unixnu UniXNU.efi …`
直接启动。

同一次运行要一并验证的两件事（都是本轮新修、都没实测过的）：

1. **QMP `screendump` 是否真的出图**。修复前它是个 no-op；修复后应当每个
   `--every` 秒产出一张 PNG，`boot-report.json` 的 `screenshots` 非空。
   如果仍然为空，`qmp_evidence.screendump_error` 会说明原因。
2. **QMP `quit` 是否让 QEMU 自己退**。修复前每次都是 `process.terminate()`；
   修复后 `exit_code` 应当来自 QEMU 自己的退出码。

帧缓冲几何：`-fw_cfg name=opt/org.unixnu/ramfb-size,string=1376x1024` **只在 ESP 上真有
`QemuRamfb.efi` 时才发**（`unixnu_boot.boot()` 检测后传给 `build_argv`），因为该 fw_cfg 项就是
由那个驱动读的（`run-qemu.py:105-116`）。所以：

- ESP 来自 runner 的 payload（带 `QemuRamfb.efi`）→ 几何被设成 1376x1024；
- ESP 由本机 `stage_esp` 现搭（不带该驱动）→ 不发 fw_cfg，帧缓冲是固件默认的 800x600，
  截图比例不对。这是已知且已如实记录的状态，不是缺陷——但**不能据此判断渲染是否正常**。

预期第一次失败点在 UniXNU 的 `unixnu.toml` 解析或 Payloads 路径上。
