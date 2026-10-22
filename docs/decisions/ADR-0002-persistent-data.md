# ADR-0002: 持久 Data 路线（H04）

- 编号：ADR-0002
- 日期：2026-10-22
- 状态：accepted (code stage) —— 路线已在代码阶段采纳；真机持久化验证 **UNVERIFIED**
- 关联需求：PRD §2.1 E03、§2.3 H04、§3.1 FR05、§4.2「Data」行、§5.2 用例 8、§5.3 数据持久化指标、§6 M9b
- 关联契约：`config/machine.json` `devices.data_disk`（`virtio-blk`，`readonly: false`）、`unipad.errors.ErrorCode.PERSISTENCE_FAILED`、`unipad.models.EvidenceSet`

## Context

1. 上游社区仓库自述 **Data 使用 RAM disk**（PRD §2.1 E03，作者自述、未独立复现）。RAM disk 的含义是：客体重启后 Data 内容丢失。
2. FR05 要求「应用安装与应用数据在正常关机重启后保留」；PRD §5.2 用例 8 进一步要求「正常关机，结束 QEMU 进程，再从磁盘启动新进程；两款 App 和保存的字符串仍存在」——即必须是**跨 QEMU 进程**的持久化，RAM disk 在原理上不满足。
3. PRD §4.2 虚拟设备策略已把 Data 列为「独立的持久写入方案」，前置验证为「客体正常写入、同步、重启与恢复均可靠」；`config/machine.json` 已预置 `data_disk: {if: virtio-blk, readonly: false}`。
4. 宿主磁盘紧张（`host-inventory.json`：C: 剩余 0.6GB、E: 0.5GB，F: 69.3GB），Data 镜像必须放在 `F:`，且容量需要独立测试（PRD §11「Data 容量/写入不可靠」风险：ENOSPC、重启丢失或卷损坏）。
5. 第一版只承诺**正常关机**的持久化；断电一致性与崩溃恢复是额外指标，不得用「完整内存快照恢复」替代本节正常重启验证（PRD §5.2 末段）。

## Decision

1. **放弃上游 RAM disk 路线**：为客体提供一块**独立的可写 Data 镜像**，挂在 `virtio-blk`、`read-only=off`，与只读系统卷（`system_disk`，`virtio-blk`，`readonly: true`）物理分离。系统卷保持只读，避免把用户数据写进需要重放的系统镜像。
2. **同步策略：优雅关机时显式 flush/sync**。宿主在 `unipad stop --graceful` 路径上：先经 QMP 请求客体关机 → 等待客体完成 Data 卷卸载/sync → 再结束 QEMU 进程。宿主**不得**以「直接 kill QEMU」作为正常关机路径；非优雅终止必须被记录为一次异常终止事件，不得用于宣称 FR05 通过。
3. **Data 镜像的位置、容量、格式与 UUID 记入配置**（`config/machine.json` 的 `disks.data_image`），并纳入 `artifact-manifest.json` 哈希清单；Data 镜像本体属于用户数据，不入 Git（PRD §9 / §12）。
4. **安装状态与数据同盘**：应用安装产物（应用容器）落在 Data 卷，因此 FR05 同时覆盖「应用安装保留」与「应用数据保留」，两者在 M9b/M11/M12 用同一机制验证但分别取证。

## Consequences

**正面**

- 跨 QEMU 进程重启后 Data 内容可保留，FR05 与 PRD §5.2 用例 8 在原理上可达。
- 系统卷只读使「回滚到已知可启动版本」（FR10，见 `docs/rollback.md`）只需替换系统侧指针，不必担心用户 Data 被回滚掉。
- 容量、同步、挂载、恢复可以被独立测试（PRD §11 对应风险的应对即「独立完成容量、同步、挂载和恢复测试」）。

**负面 / 代价**

- 客体侧必须真正把 Data 卷当持久卷挂载并使用；若客体仍按 RAM disk 语义初始化 Data，则本路线不成立，需要回到 H04 的「实现持久 Data 路线」分支——**这是现场阻塞项，不是代码可以假装的**。
- 宿主磁盘需要为 Data 镜像预留空间（F: 当前 69.3GB 可用），需要先测 IPSW/RootFS 尺寸再定容量。
- 优雅关机链路引入新的失败模式：客体不响应关机请求、QMP 超时、sync 未完成。这些必须以 `qemu_timeout` / `PERSISTENCE_FAILED` 明确分类，不得静默降级为 kill。
- 断电一致性不在第一版承诺内；若现场发现非优雅关机导致卷损坏，只能记为额外指标缺口，不得反向宣称正常关机验证失败或成功。

**不做的事**

- 不用 RAM disk + 关机导出脚本冒充持久 Data。
- 不用「快照恢复」替代正常关机重启验证（PRD §5.2）。

## Status

accepted (code stage)。代码阶段可完成：Data 镜像的参数组装、只读/可写断言、优雅关机序列（QMP → 等待 → 结束进程）、异常终止记录与 `PERSISTENCE_FAILED` 分类。**真机持久化（M9b）未执行，标记 UNVERIFIED**；宿主磁盘上的 Data 镜像实际行为依赖客体挂载语义，属现场验证。

## Evidence-required

M9b 通过必须留存（缺一即 `EVIDENCE_INCOMPLETE`，不得判 PASS）：

| 证据 | 说明 |
| --- | --- |
| Data 卷挂载证据 | 客体侧挂载对象、UUID、只读属性与实际写入路径 |
| 随机标记写入记录 | 写入前的随机标记值（本次唯一）与写入时间戳 |
| 优雅关机证据 | QMP 关机请求/响应日志、客体卸载或 sync 证据、QEMU 进程退出码 |
| 全新 QEMU 进程启动记录 | 新进程 PID、启动命令、实际 `-accel` 值 |
| 读回结果 | 标记值逐字节比对结果；不一致时保留现场而不覆盖上次失败（PRD §5.2 用例 9） |
| 三轮重复 | PRD §5.3 要求「正常关机重启 3 次均保留，每次使用新随机标记」 |
| 容量/错误注入 | ENOSPC 与写失败时的客体行为与错误分类 |
