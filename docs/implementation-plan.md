# UniPad 代码阶段实施计划（2026-10-23 更新）

本文件记录本轮代码阶段的推进结构与四字段约定（落实项目 CLAUDE.md「代码阶段与现场阶段分离」）。

## 推进轮次

| 轮次 | 内容 | 推送 |
| --- | --- | --- |
| R0 | git 基线：PRD 入 docs/、host-inventory.json（WP00 真实采集）、目录约定 | ✅ 已推送 |
| R1 | 脚手架与共享契约：errors.py / models.py / pyproject / config 模板 | ✅ 已推送 |
| R2 | Workflow#1：并行实现 controller / host / ipa / guest / scripts / workflows / docs | ✅ 已推送（7cf342b / 97014b0） |
| R3 | Workflow#2：fakes + 单元测试 + M0–M4 与 IPA-MVP 模拟验收套件（含 6 处缺陷修复） | ✅ 已推送（a391498） |
| R3b | 修好文档化但失效的 `python -m unipad` 入口（补 `unipad/__main__.py` + 回归测试） | ✅ 已推送（5d67b06） |
| R4 | 全量测试运行与修复（Windows 本机 pytest，520 项） | ✅ 完成 |
| R5 | Workflow#3 五维审计：反向 / 性能 / 规范 / 调用链 / 对质（变异测试） | ✅ 变异轮完成（15/15 变异均被发现，无测试空洞） |
| R5b | 修复审计确认的 CRITICAL 发现 RR-01（安装门禁把 failed 当成功）+ ADR-0007 + 版本升版 | ✅ 已推送（ed4414d） |
| R5c | M0 源身份固定：sources.lock.json 填实提交/IPSW 哈希，解决 21H161/21H461 build 号分歧 | ✅ 已推送（f0c106a） |
| R6 | Workflow#4 + #5：Windows 真实启动关键路径（工具链 / IPSW 抽取 / LZFSE 解码器 / XNU 可移植性 / QEMU argv / prelink / EFI / ESP / OS-root） | ✅ 已推送（97d40f6 / 2353718） |
| R6b | launcher 与板级对齐：pflash 固件、board 一致的内存/CPU/屏幕、`-no-reboot`、`-fw_cfg` ramfb-size、修 `fat:` 误判缺失 | ✅ 已推送（2353718，12/12 变异被抓） |
| R6c | **真实启动**：`-kernel` 启动模式 + AArch64 裸机固件（本机 clang 交叉编译）+ 真 QEMU 端到端验收 + 修 ANSI 代码页路径缺陷 | ✅ 已提交（4451826，6/6 变异被抓；推送待网络恢复） |
| R7a | P1(3) 安装生命周期：append-only 历史、卸载/更新为转移、封闭错误分类表；并修掉 store 失败被静默吞掉的缺陷 | ✅ 已提交（86de33c / 3ff2314，8/8 + 1/1 变异被抓） |
| R7 | P1 剩余代码实现（Windows 可编码部分）+ 全量测试 + 审计 | 进行中 |
| R8 | 终验、推送、审计验收报告（含现场未验证项清单） | 待 |

## 四字段记录（每个阶段必须分别记录，不得互相冒充）

| 阶段 | 实现状态 | Windows/模拟测试状态 | 现场验证状态 | 阻断原因 |
| --- | --- | --- | --- | --- |
| WP00 基线冻结 | CODE COMPLETE（R0/R1/R5c/R6：源身份与工具身份均已按证据固定并带摘要） | PASS（host-inventory.json 真实采集、doctor 可运行、锁文件提交为 40 位 SHA + QEMU/clang 版本与 sha256） | UNVERIFIED | 现场=用户本机；`frozen` 仍为 false（clang 未证明可交叉编译 iOS、未跑过干净构建） |
| WP01 干净构建 | UNVERIFIED（需 macOS runner + 冻结版本） | 模拟（CI preflight 可验证输入契约） | BLOCKED | `sources.lock.json` 未 frozen；Actions 预算未授权；本机无 Xcode |
| WP02 Windows UEFI | 代码完成（启动器/参数/QMP/日志；pflash + `-fw_cfg` + `-no-reboot` 已按板级补齐） | 模拟 + **真 QEMU 11.1.0 逐条 argv 验收**（QMP 应答 `prelaunch`） | BLOCKED | `kernel.macho` 无法在本机产出（见「Tier-1 启动关键路径现状」） |
| WP03 引导交接 | 代码完成（证据采集/实验记录） | **PASS（真实客体）**：AArch64 裸机固件在真 QEMU 11.1.0 TCG 下启动，串口标记、QMP ready、screendump、优雅退出全部由本 launcher 驱动 | UNVERIFIED | 该真实客体不是 iPadOS/XNU；EDK2→UniXNU→XNU 交接仍 BLOCKED |
| WP04 内核进入 | 代码完成（配置/故障分类） | 模拟（故障注入） | BLOCKED | 依赖 WP03；`emit_prelink` 需要打过补丁的 XNU，本机构建被 availability.pl / xcrun / ditto / 缺 iOS sysroot 阻断 |
| M10–M12 IPA 链路 | 代码完成（预检/安装状态机/协议/fake guest） | 模拟（FakeGuestService 全流程 + 故障注入） | BLOCKED | 无真实客体安装服务 |
| 验证器逻辑 | CODE COMPLETE（R5b：install.stage_terminal 改判成功终态，VERIFIER_VERSION 1.1.0） | PASS（新测试先在旧实现上失败、再经变异复现，均已恢复） | UNVERIFIED | 真机安装门禁表现需现场各跑一次成功/失败 |
| CI 流水线 | CODE COMPLETE（4 个 workflow） | 模拟（结构与输入校验可本地检查；真跑需 GitHub） | UNVERIFIED | Actions 运行需推送后观察 |

## 判定规则

- CODE COMPLETE ≠ 现场 PASS；SIMULATION PASS ≠ 现场 PASS。
- 现场（用户 Windows 本机启动 iPadOS）未执行前一律 UNVERIFIED/BLOCKED，并在最终报告列明。
- 任何「成功」必须有证据：实验记录 + 产物哈希 + 运行日志 + 验证器结论；证据缺失即未验证。

## 本轮新确立的事实（均有证据，未经现场验证不得外推）

1. **IPSW 已到位且哈希吻合**：`iPad_Pro_HFR_17.7.11_21H461_Restore.ipsw`，6,835,124,840 字节，sha256 `c84ad24b…45a1`；board.toml 钉住的两张 dmg 哈希与实测一致。
2. **build 号分歧已解决为 21H461**（三份独立证据，见 `config/sources.lock.json` 的 `system_identity.resolution`）。
3. **XNU 内核构建不一定需要 xcodebuild**：`scripts/build-xnu-4k-baseline.sh` 全文没有调用 xcodebuild，实际是 `make -C .cache/xnu-work build SDKROOT=iphoneos …` + clang；`kernel.py:75` 的 `tool('xcodebuild')` 只是存在性检查。脚本里的 macOS 专有项是 `ditto`（3 处）与 `build-firehose.sh`（xcodebuild + xcrun lipo），而后者只在 `libfirehose_kernel.a` 不存在时才运行（第 122 行）。
4. **本机构建工具链已可用**（工具链探针实测，全部在 F:、均不在 PATH）：QEMU 11.1.0；MSYS2 ucrt64 clang/clang++ 23.1.3、ld64.lld 23.1.3、GNU Make 4.4.1、cmake 4.4.4、ninja 1.13.2、unifdef 2.12、mtools 4.0.49（mformat/mcopy）、gzip 1.15。**交叉编译已证明而非假设**：`clang -target arm64-apple-ios17.0 -c` 产出真 Mach-O arm64 对象（反汇编见 `d10043ff sub sp,sp,#0x10` 等真实 AArch64 指令），且 **ld64.lld 成功链接出真 Mach-O arm64 可执行文件**（链接这半此前未证明）；clang 也接受 XNU 的 DEPLOYMENT_TARGET_FLAGS 与默认 EXTRA_CFLAGS。另用 clang 造了真的 `python3.exe` PE32+ shim（`.cmd` shim 会被 `CreateProcess` 拒绝，`subprocess.run(['python3',…])` 报 WinError 2）。
5. **完整 iPadOS 启动（`--os-root`）在 Windows 上结构性 BLOCKED**：上游 `run-qemu.py` 调用 hdiutil 并把 `/dev/rdisk` 作为 QEMU host_device 传给 QEMU，Windows 无等价物。
6. **真机 Tier-1 启动的唯一硬阻断是 `kernel.macho`**（三个探针一致结论）：`emit_prelink.py:157` 是 `output = bytearray(native_data)`，即 `--kernel` 传进去的镜像**就是**要启动的镜像，没有任何路径能让启动代码来自别处；且原版 kernelcache 缺 `_kalloc_sparse_prelink_index_version`（`emit_prelink.py:123`）、`__PRELINK_TEXT` 等段非空而 `append_segment` 要求空占位（`emit_prelink.py:166-168`）、kext 偏移与内核自带 prelink 段冲突（`native_prelink.py:237`）。`kernel.py:120-122` 在 XNU 缺失时直接报错。
7. **ESP 不需要 mtools 也能做，但本机其实有 mtools**：`msys64/ucrt64/bin` 带 mtools 4.0.49，只是不在 PATH，上游 `payload.py` 可**原样**运行。QEMU `fat:<dir>` 由暂存目录合成 FAT 卷，`payload.img` 只在 `run-qemu.py:90` 被读回一次（就是为了建那个目录）。
8. **`.dmg` 性质已查清**：`044-76040-038.dmg` 是 UDIF（`koly` 尾 + `blkx`/`mish` 块映射 + GPT + 内层 `bvx2`/`bvxn` 块），QEMU 11.1.0 自带 dmg 驱动**拒绝**读取它（`sector count … larger than max`，且此构建缺 `dmg-lzfse` 模块）；`044-76034-038.dmg` 是**裸 APFS**（偏移 32 处 `NXSB`，无 `koly`、无 GPT）——这正是 `run-qemu.py:199-207` 对它用 `driver: file` 而不调 hdiutil 的原因。纯 Python UDIF→raw 转换已证明可行（块映射逐字节交叉验证通过，99.7% 已可解）。
9. **`availability.pl` 阻断已证明可解**：它就在盘上（`refer/AvailabilityVersions/`，81 字节 `/bin/sh` 包装器 + 21,582 字节 **Python 3** 程序 + `availability.dsl`）。XNU 只用到 `--ios` / `--macosx` 两个开关（`bsd/sys/make_symbol_aliasing.sh:79,97`），重的 `--preprocess` 路径内核构建根本不走。修法已端到端验证：把三个文件拷进 `<fakeroot>/usr/local/libexec/`、改 shebang 为 `python`、用 `XNU_AVAILABILITY_PL` 指过去，`_symbol_aliasing.h` 正常产出 **907 行 / 292 个 `__DARWIN_ALIAS_STARTING_*` 宏**。**陷阱**：只装 81 字节包装器而不装 `availability` 与 `availability.dsl`，脚本会**退出 0** 但只写出 31 行零宏的头文件——静默失败。
10. **XNU 全量构建的硬阻断是 `mig` 与 `iig`，本机无源可依**：`mig`（15 处使用，约 40 个 .c + 45 个 .h）与 `iig`（DriverKit）在 F: 盘上只有**运行时**文件（`mig_allocate.c` 等）与**输入**（`osfmk/mach/*.defs`），没有生成器本体；msys64 与 zig 里也没有。本机无网络，无法获取。已亲自复核：`find` 全盘无 `mig.py` / `migcom` / `iig`，`libmigism.a` 是 LLVM 的迁移库、与 Mach MIG 无关。**结论：在本机从零写出一个 MIG 编译器是唯一剩余路径，工作量以周计，且无上游依据。**
11. **其余 macOS 专有项均有替代**：`ditto`×3 → `cp -R`；`xcrun`（26 次 shell 调用）→ make 命令行传 `XCRUN=`；`sw_vers` 已有 shim；`gm4` → 链到 `m4`；`HOST_CODESIGN` → `true` 桩；`plutil` → Python `plistlib`；`strip`/`nm`/`dsymutil` → `llvm-strip`/`llvm-nm`/ucrt64 `dsymutil`；`lipo` 单架构构建可避开；CTF 四项设 `DO_CTFMERGE=0` 即关。另发现一个 **msys make 的 VPATH bug**（`makedefs/MakeInc.def:1002`，`VPATH = .:$(SOURCE)` 在 msys make 下破坏隐式规则），一行 `export VPATH = ./:$(SOURCE)` 可修。
12. **LZFSE 解码器已证明可用**：纯 stdlib Python（`local-private/out/lzfse/im4p_lzfse.py`）把 IPSW 的 kernelcache IM4P 解成合法 arm64 Mach-O，46,415,872 字节，magic `0xfeedfacf`，cputype 12。

## Tier-1 启动关键路径现状（2026-10-23）

| 文件 / 步骤 | 状态 |
| --- | --- |
| `qemu-system-aarch64.exe` 11.1.0 | **PRESENT** |
| `share/edk2-aarch64-code.fd`（64 MiB） | **PRESENT** |
| `<run>/vars.fd` | MISSING-PRODUCIBLE（launcher 已实现，按 code 尺寸截断） |
| `kernel.macho.adt` | MISSING-PRODUCIBLE（纯 Python `make-qemu-adt.py` + `dumpdtb`；**不读 IPSW 的 DeviceTree IM4P**，100% 合成） |
| `unixnu.toml` | MISSING-PRODUCIBLE（文本模板） |
| `UniXNU.efi` / `DarwinBoot.efi` / `QemuRamfb.efi` | MISSING-PRODUCIBLE（需 EDK2 BaseTools + CLANGPDB；clang-cl/lld-link/make 已在本机） |
| **`kernel.macho`（打过补丁的 XNU）** | **MISSING-BLOCKED —— macOS Xcode iphoneos SDK / availability.pl / xcrun / ditto** |
| `payload.img` | MISSING-BLOCKED（本机有 mtools，但仍被 kernel.macho 挡住） |

**结论：本机真实启动 Tier-1 内核目前被 `kernel.macho` 单独阻断。** argv 本身已逐条被真 QEMU 11.1.0 接受（`-M virt,virtualization=off,secure=off,gic-version=3`、两个 64 MiB pflash、`fat:<空目录>`、`virtio-blk-pci`、`ramfb`、`qemu-xhci`、`usb-kbd`、`-nic none`、`-display none`、`-no-reboot`、`-serial file:`、`-qmp tcp:…,server=on,wait=off`、`-fw_cfg …ramfb-size`，QMP 应答 `prelaunch`）。
