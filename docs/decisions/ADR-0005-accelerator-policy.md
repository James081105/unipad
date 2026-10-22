# ADR-0005: 加速器策略（Accelerator Policy）

- 编号：ADR-0005
- 日期：2026-10-22
- 状态：accepted (code stage) —— 策略已采纳；WHPX 探测与实测性能 **UNVERIFIED**
- 关联需求：PRD §2.1 E05、§2.2-1、§3.1 FR02、§4.2「CPU」行、§6 M4/WP04、§8.3
- 关联契约：`config/machine.json` `accel` / `accel_note`、`unipad.models.MachineConfig.accel`、`unipad.errors.ErrorCode.ACCELERATOR_NOT_TCG`、`unipad.errors.ErrorCode.QEMU_LAUNCH_FAILED`

## Context

1. PRD §2.1 E05：QEMU TCG 提供跨 CPU 架构模拟能力，支持 AArch64 系统模式；Intel 主机选择 TCG，但**实际系统兼容性仍由门禁确认**。
2. PRD §2.2-1 固定判断：Intel 的 VT-x/Hyper-V **不会直接执行 ARM64 iPadOS 指令**；本机选用 QEMU TCG，其速度必须实测，**不能套用 Apple Silicon HVF 成绩**。
3. `host-inventory.json`（WP00 真实采集）记录本机 **Hyper-V 已启用**（`cpu.hypervisor_present: true`），并注明「WHPX availability must be probed; QEMU must run with explicit TCG (`-accel tcg`) and must not silently fall back to WHPX/HVF. Record the actual accelerator at runtime.」
4. 关键约束：WHPX 只在 x86_64 客体上可用，**对 AArch64 系统模拟无效**。因此探测 WHPX 的动机是「确认 Hyper-V 与 TCG 共存时是否有内存分区/性能冲突」，而不是「换用更快的加速器」。
5. PRD §8.3：**禁止把 HVF payload 当成 TCG payload 下载到本机直接使用**；即使 runner 为 ARM64，也不默认存在所需 HVF 嵌套虚拟化条件。`unipad.models.ArtifactManifest.target` 已含 `tcg` 字段与相应注释。

## Decision

1. **本机唯一默认加速器是 TCG**，且必须在 QEMU 命令行**显式**写出（`-accel tcg`）。`config/machine.json` 的 `accel` 固定为 `tcg`，任何其他值在启动前被拒绝并返回 `ACCELERATOR_NOT_TCG`。
2. **禁止静默替换**：启动器不得依赖 QEMU 的默认加速器选择，不得在 TCG 启动失败后自动改试 WHPX/HVF。若配置或被请求的加速器不是 `tcg`，直接失败（Fail-fast，PRD 箴言 38/39）。
3. **WHPX 在 M0 探测，但永不默认、永不静默启用**：探测只回答「Hyper-V 存在时 TCG 能否稳定运行、是否存在内存分区或性能冲突」。探测结果记入 `host-inventory.json` 与实验记录的 `runtime.accel` 字段；即使探测显示 WHXP 可用，AArch64 客体的加速器仍保持 `tcg`。
4. **运行时记录实际加速器**：`ExperimentRecord.runtime.accel` 与运行证据必须记录本次 QEMU 实际使用的加速器（从 QEMU 参数与日志取得），不得沿用配置值充当证据。
5. **payload 分流**：macOS Actions 产物必须明确标注面向 TCG 还是 HVF（PRD §8.1 平台信息）；Windows 侧下载与校验环节**拒绝** `target != "tcg"` 的产物，拒绝时给出明确原因而非静默使用旧包。
6. **性能不外推**：任何性能结论必须来自本机实测；不得引用 Apple Silicon + HVF 的数字作为本机预期（PRD §2.2-1、§5.3）。

## Consequences

**正面**

- 启动参数可预测、可复现；「实际加速器是什么」永远是证据问题而非推断问题。
- Hyper-V 共存问题在 M0 被显式暴露，不会以「莫名很慢/偶发失败」的形式晚晚出现。
- 杜绝把 macOS/HVF 产物误用到 Windows 导致的虚假通过。

**负面 / 代价**

- 明确放弃任何潜在的加速路径：TCG 是本机唯一选项，性能不足时只能优化客体/QEMU 配置或记录阻塞，不能「换个加速器试试」。
- 显式 `-accel tcg` 可能在 Hyper-V 环境下触发额外限制（内存分区、时序），需要 M0/M4 实测并记录，这些是**现场项**。
- WHPX 探测本身不产生性能收益，属于必须做但无直接回报的工作。

**不做的事**

- 不在 AArch64 客体上启用或默认 WHPX（无效且会掩盖真实失败）。
- 不用 HVF 产物冒充 TCG 产物。
- 不把「QEMU 支持 ARM64」推导为「本机 TCG 一定成功」。

## Status

accepted (code stage)。代码阶段可完成：`accel` 校验与 `ACCELERATOR_NOT_TCG` 拒绝、显式 `-accel tcg` 参数组装、产物 `target` 校验、运行时加速器记录字段。**WHPX 探测（M0）与 TCG 实测性能/稳定性（M4、H05）未执行，标记 UNVERIFIED**。

## Evidence-required

| 阶段 | 证据 |
| --- | --- |
| M0 | WHPX 探测结果（探到/未探到/探到但不可用）、Hyper-V 状态、结论：是否与 TCG 共存冲突 |
| M1/M4 | 实际 QEMU 命令行（含 `-accel tcg`）、QEMU 版本与构建来源、本机 CPU/内存实测 |
| H05 | 本机交互延迟、内存峰值、稳定性数据（3 次冷启动计时，PRD §5.3） |
| 产物校验 | Actions 产物的 `target == "tcg"` 断言、逐项哈希与架构核对 |
