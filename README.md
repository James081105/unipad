# UniPad

**Run real iPadOS userland on an Intel Windows host via QEMU AArch64 TCG + UniXNU, then install and interact with DRM-free ARM64 Device IPAs.**

> 状态：代码阶段进行中。真机验收（在用户 Windows 本机启动 iPadOS）尚未执行，标记为 UNVERIFIED。
> PRD：[`docs/PRD.md`](docs/PRD.md)（UniPad PRD v0.1，2026-10-22）

## 先说清楚两件事

**一、这个仓库不含任何 Apple 材料。** IPSW、内核缓存、设备树、trust cache、Data 种子、私钥全部不在库里，也不在任何一个提交里（`local-private/` 与 `vendor/` 都被 `.gitignore` 覆盖）。要跑起来，你需要**自备** iPadOS 17.7.11（build 21H461）的 IPSW，放进 `local-private/ipsw/`；其余都由 `config/sources.lock.json` 固定的提交构建或推导。这也是本项目对 PRD §12 的执行方式。

**二、到 2026-10-26 为止，实测走到哪一步。** 下表只写**量到过的**，没量到的一律写未验证，不拿推断冒充证据：

| 项 | 状态 |
| --- | --- |
| `qemu_root_defconfig` 内核构建 | **已实测**（macOS runner，run 40241617618 / 40250055465，`stages: ran=7 not_run=0 failed=0`） |
| 本机从 IPSW 解出 OS 卷（UDIF→raw，纯 Python LZFSE） | **已实测**：6,077,580,800 字节，GPT + APFS 容器自相一致 |
| 根卷挂载 | **已实测**（Windows QEMU TCG）：`mount-complete volume DawnUpdate21H461.J120J121J207J208OS`，卷 UUID `04769D26-…` 与 board.toml 逐字符相同 |
| PID 1 | **已实测**：`init` pid 1 在跑；内核日志在 `No POST boot-arg set.` 之后停止增长 |
| trust cache 链路 | **已实测**：内核 `attempting to load 2 external trust cache modules` → `completed loading external trust cache modules` |
| **原始 launchd 作为 PID 1** | **未验证**。阻断点已用 PC + 内核栈定位并**前移过两次**。第一次测到：访客 panic 在 `panic_spin_forever()`（PC `0xfffffff00b8653fc`），帧链 `bsd_ast → bsdinit_task → load_init_program → panic_trap_to_debugger`，栈上实参块给出 `offline owner SystemOS mount failed: 80 (EAUTH) @kern_exec.c:8032`——`native_owner_cache_prepare()` 找不到卷 UUID `8846FE78-2910-4297-AF97-D59BB6F8FC16`，因为本机只接了 ESP 与 OS 根卷。**补上 SystemOS cryptex（`044-76034-038.dmg`，裸 APFS）作为第三个 VirtIO 只读块设备后，该挂载成功**。**再补上 packed Data ramdisk 的两半（ADT 的 `/chosen/memory-map/PackedRAMDisk` 16 个零字节占位 + `unixnu.toml` 的 `initrd`）后，`NativeRamBlock` 起来并发布了 1 GiB 块设备**：`_native_ram_block_ready` 1、`_native_ram_block_bytes` 1073741824、`_native_ram_block_resident_bytes` 36864、`sparse` 1、`reads` 10、`failures` 0，控制台 `disk4 … block count 262144`。**当前阻断点是种子内容，不是接线**：APFS 拒绝这个容器（`nx_corruption_detected_int … nx_check_superblock` / `disk4 superblock failed sanity checks: 92`），而本机这个 `owner-data-1g.dbr`（36,960 字节）是一个手写占位 stub——`nx_uuid` 就是按顺序递增的 `00 01 02 … 0e 0f`，runner 上那一个是 1,245,328 字节，本机没有 |
| SpringBoard / 输入 / 持久 Data / IPA 安装 / 十条验收 | **未验证** |

> 前两版这里先后把阻断写成「OS 卷快照元数据树是空的」和「内核停在 `mach_vm_allocate_kernel`」，**两者都被测量否证**：前者被日志顺序否证（`failed to find named root snapshot` 打在 18.873 秒，40 毫秒后 `mount-complete` 照常出现，非致命）；后者被帧指针链否证（`load_init_program` 确实跑了，且它内部先执行的是 `native_owner_cache_prepare()`，不是分配）。**设备仓库自己为这个 panic 准备了 checker**（`vendor/UniXNU/scripts/check-owner-systemos-mount-checkpoints.py`），并把拒绝态与成功态的全局量写成断言——那两组值就是下一步的验收判据。

完整的测量记录、每一次失败的归因，以及「哪些是代码完成、哪些是模拟通过、哪些是 runner 验证、哪些是本机实测」四种状态的分辨，在 `docs/implementation-plan.md` 与 `docs/audit-r8.md`。

## 当前基线

| 项 | 值 |
| --- | --- |
| 目标宿主 | Windows 11 Home, Intel i7-11800H 8C/16T, 16GB RAM, RTX 3060 Laptop |
| 加速器 | QEMU **TCG（显式）**；Hyper-V 已启用，WHXP 需在 M0 探测 |
| 客体候选 | iPad7,1 / iPadOS 17.7.11 / build 21H161（以 `config/sources.lock.json` 固定提交为准） |
| 宿主盘点 | [`host-inventory.json`](host-inventory.json)（WP00，真实采集） |

## 仓库结构（实施设计见 PRD §9）

```
config/       sources.lock.json, machine.json, budget.json（拟建，模板见 config/README.md）
src/unipad/   controller（状态机/实验/预算/验证器/CLI）+ host（QEMU/QMP/镜像/日志/输入）+ ipa（预检/安装）+ guest（协议/客户端/假服务）
scripts/windows/  PowerShell 入口：盘点、下载校验、启动、诊断
scripts/macos/    macOS 构建适配入口（仅 macOS 执行，Windows 上为文档化存根）
tests/        单元测试 + fakes（假 QEMU/假 QMP/假安装服务）+ acceptance（M0–M4 与 IPA-MVP 模拟门禁）
.github/workflows/  preflight / build-macos / verify-artifacts / publish
docs/decisions/    版本、持久 Data、安装通道、图形路线决策记录
runs/<experiment-id>/  运行证据（不入 Git）
artifacts/      本地构建产物与已知可启动版本指针（大二进制不入 Git）
local-private/  IPSW、客体镜像、种子、密钥（**绝不入 Git**）
```

## 开发约定

- Python 3.12 + pytest；包管理见 `pyproject.toml`。
- 每次实验记录遵循 PRD §7.3 `experiment.json` 格式；证据最小集合见 §9.2。
- 禁止事项（PRD 附录 A）：不得把缺失证据补写为成功、不得用旧包冒充当前构建、不得把 mockfs PID 1 当系统 launchd、不得把预装图标或静态画面当 IPA 运行。
- Git commit message 使用英语（见 `CLAUDE.md`）。

## 快速开始（代码阶段，Windows）

```powershell
python -m pip install pytest        # 已随本机 Python 3.12.9 提供则跳过
python -m pytest tests -q           # 运行全部单元与模拟验收测试
python -m unipad doctor             # 宿主与工具链盘点（WP00 复查）
```

> 本机注意：当前 Windows 用户名含非 ASCII 字符，`pip install -e .` 会失败
> （setuptools "Unexpected condition testing for writable directory"）。
> 已改用 `.pth` 方式完成可编辑导入：site-packages 下 `unipad.pth` 内容为
> `F:/iosvirtual/src`。新环境复现时执行：
> `python -c "import site;print(site.getsitepackages()[0])"` 得到目录后写入同名 `.pth`。
