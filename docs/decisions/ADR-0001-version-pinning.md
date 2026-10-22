# ADR-0001: 版本固定策略（Version Pinning）

- 编号：ADR-0001
- 日期：2026-10-22
- 状态：accepted (code stage) —— 代码阶段已采纳；真机现场验证 **UNVERIFIED**
- 关联需求：PRD §2.1、§3.1 FR01、§6 WP00、§8.1、§9 `config/sources.lock.json`
- 关联契约：`unipad.models.ArtifactManifest`、`unipad.errors.ErrorCode.ARTIFACT_MANIFEST_INVALID` / `ARTIFACT_MISSING` / `IMAGE_HASH_MISMATCH` / `CONFIG_INVALID`

## Context

PRD §2.1 说明本轮只核查了公开文档与配置文件，**没有在用户电脑复现**；附录 B 明确「上游分支会变化，工程基线以实际提交和文件哈希为准」。因此：

1. README、`board.toml`、`unixnu.lock` 都是上游可变内容；任何以「当前 main」为前提的结论都不可复现（PRD §11「构建不可复现风险」）。
2. 仓库已提供 `config/sources.lock.json`（PRD §9 目录设计），但其中 `unixnu.commit`、`device_repo.commit`、`ipsw.sha256`、`tools.qemu.version` 均为 `null`，`frozen: false`。WP00（M0）之前不能据此宣称可构建。
3. **已知的 build 号差异（开放项）**：PRD 附录 A 的启动指令写作 build `21H161`，而 PRD §2.1 E04 与 `host-inventory.json` 的 `open_items` 写作 `21H461`。两者不可能同时是 iPad7,1 / iPadOS 17.7.11 的正确 build。该差异**未解决**，必须在 M0 从**固定提交的 `board.toml`**（以及实际 IPSW 的 `BuildManifest.plist`）核对后写入 `config/sources.lock.json` 的 `system_identity` 字段。
4. 上游明确要求 Apple IPSW、kernelcache、镜像、trust cache、Data seed 与厂商固件**不放入源码仓库**（PRD §12，[S1]）。版本固定与素材分发必须分开处理。

## Decision

1. **单一解析源**：每次实验的系统版本、代码版本、子模块/feeds、IPSW 与工具链，一律从 `config/sources.lock.json` 声明的**具体提交**解析；控制器不允许读取 `latest`、`main` 或任何会漂移的引用。`ExperimentRecord.base_commit` / `candidate_commit` 与 `source_lock_sha256` 必须非空（PRD §7.3）。
2. **禁止移动分支构建**：构建入口只接受提交 SHA；若锁文件为 `frozen: false` 或存在 `null` 必填项，构建必须返回 `blocked` / `CONFIG_INVALID`，不得「先跑一遍看看」。
3. **build 号差异作为开放项记录，不猜测**：在 M0 之前，代码、文档、日志中涉及 build 号的地方必须写成 `21H161 或 21H461（待 M0 从固定 board.toml 解析）`，不得单选一个当作事实。解析结果连同证据（board.toml 内容哈希、IPSW SHA-256、BuildManifest 摘录）写入锁文件 `system_identity.build_candidates` 与实际值字段。
4. **`frozen=true` 的门槛**：只有锁文件中每个 `null` 都被证据填实、且 PRD §6 M0 的通过证据（固定提交/工具/配置、清洁构建产物及哈希）齐备后，才允许把 `frozen` 置为 `true`，并在实验记录中留下改动人、时间与证据索引。验证逻辑的修改独立记录，不得随功能修改偷偷放宽（PRD §7.4）。
5. **哈希先于运行**：产物存在、尺寸非零、哈希/架构/清单正确后才进入运行（PRD §7.4）；下载失败不得继续使用未标记的旧包。

## Consequences

**正面**

- 任何一次运行都可以从「锁文件 + 记录的哈希」重建，满足 FR01「可从版本清单重建；所有运行文件有来源和哈希」。
- build 号差异被显式暴露为开放项，避免把错误系统身份带到 M4/M7（PRD §11「镜像/系统不匹配」风险：dyld、UUID、哈希或平台服务失败）。

**负面 / 代价**

- M0 之前**无法启动真实构建**：WP01 干净构建与 WP02 Windows UEFI 在现场侧依赖已冻结的锁文件。代码阶段只能用 fake/模拟验证输入契约。
- 每次上游交互都要重新解析并记录提交，操作步骤变多；不能「顺手更新到最新」而不改动锁文件与实验记录。
- build 号一旦在 M0 被解析并冻结，后续变更属于「系统版本变化」，按 PRD 文档变更规则需单独说明证据与影响，不得静默替换。

**不做的事**

- 不承诺上游演示成功即本机成功（PRD §1 立项判断）。
- 不把「已固定版本」当作「已在本机构建或运行」——后者仍是现场项。

## Status

accepted (code stage)。锁文件当前 `frozen: false`；build 号差异（21H161 vs 21H461）为**未解决开放项**，指定在 M0 关闭。真机（用户 Windows 本机）上的可复现构建验证 **UNVERIFIED**。

## Evidence-required

提升 `config/sources.lock.json` 到 `frozen: true` 前必须留存：

| 证据 | 说明 |
| --- | --- |
| `unixnu.commit`、`device_repo.commit` | 实际解析出的提交 SHA，非分支名 |
| `submodules` / `feeds` | 逐项 SHA；`board.toml`、`unixnu.lock` 的内容 SHA-256 |
| `system_identity` | `board` / `system` / **解析后的唯一 build 号** / 解析依据 |
| `ipsw.sha256` | 实际 IPSW 的 SHA-256（素材本体留在 `local-private/`，不入 Git，PRD §12） |
| `tools.qemu` | 原生 Windows `qemu-system-aarch64` 的确切版本与构建来源；`accel` 记 `tcg` |
| 清洁构建产物清单 | 逐项哈希与架构（PRD §8.1 产物契约），写入 `artifact-manifest.json` |
| 锁文件冻结记录 | 改动人、时间、`source_lock_sha256`、对应 `experiment.json` |
