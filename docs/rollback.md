# 回滚程序（FR10）

- 文档：`docs/rollback.md`
- 日期：2026-10-22
- 状态：accepted (code stage) —— 程序与契约已定义；真机恢复验证 **UNVERIFIED**
- 关联需求：PRD §3.1 FR10、§5.2 用例 9、§7.2 状态机与「运行版本指针和磁盘恢复与源码分支管理分离」、§9.1 `unipad restore --checkpoint <id>`、§9.2、§11「AI 错判和失控迭代」风险
- 关联契约：`artifacts/known-good.json`、`unipad.models.ArtifactManifest` / `GateResult` / `EvidenceSet`、`unipad.errors.ErrorCode.{RESTORE_FAILED, ARTIFACT_MANIFEST_INVALID, ARTIFACT_MISSING, IMAGE_HASH_MISMATCH, EVIDENCE_INCOMPLETE, CONFIG_INVALID}`、`unipad.errors.ExitCode`

## Context

1. FR10 要求「可回到已知可启动版本：使用**经过校验的历史产物**和**磁盘副本**恢复」——两个条件都要满足，缺一不可。
2. PRD §7.2 明确：实验失败时保留修改和日志，下一次从**已知基线**创建隔离分支/工作目录；**运行版本指针和磁盘恢复与源码分支管理分离，避免直接覆盖用户工作目录**。
3. PRD §5.2 用例 9 要求「重复冷启动流程三次……测试失败时保留证据，**不覆盖上次失败**」。因此回滚操作本身也必须是可追溯、非破坏性的。
4. `config/machine.json` 已声明 `disks.known_good_pointer: "artifacts/known-good.json"`，`artifacts/README.md` 说明大二进制不入 Git、产物放 F:（C: 仅剩 0.6GB）。
5. 用户 Data 属于用户资产（PRD §12 代码与私有运行素材分离；§9.2 日志不含用户文档）。「回到已知可启动版本」绝不能被实现成「顺手清掉用户的 Data」。

## Decision

### D1. 已知可启动指针：`artifacts/known-good.json`

单一事实来源，记录**当前**已知可启动检查点与历史检查点列表。建议字段（代码阶段按此实现，`schema_version: 1`）：

```json
{
  "schema_version": 1,
  "updated_at": "2026-10-22T00:00:00+08:00",
  "current": {
    "checkpoint_id": "kg-<unique-id>",
    "milestone": "M4",
    "source": {
      "unixnu_commit": "<sha>",
      "device_repo_commit": "<sha>",
      "source_lock_sha256": "<sha256 of config/sources.lock.json>"
    },
    "artifact_manifest_sha256": "<sha256 of artifact-manifest.json>",
    "artifact_run_id": "<github-run-id>",
    "data_snapshot_id": "<id or null>",
    "gate": {"verifier_id": "<id>", "passed": true, "checked_at": "<ISO-8601>"},
    "evidence": ["runs/<experiment-id>/verification.json"]
  },
  "history": []
}
```

规则：

- `current.passed` 只能来自**通过门禁**的实验（PRD §8.2 步骤 6：只有门禁通过才更新本地可启动版本指针）。
- 指针更新是**追加式**：旧 `current` 移入 `history`，不得原地覆盖后丢失历史。
- 指针只存哈希与标识，**不存大二进制、不存密钥、不存用户文档内容**。

### D2. 源码分支与磁盘指针分离（PRD §7.2）

| 维度 | 归属 | 规则 |
| --- | --- | --- |
| 源码改动 | Git 分支 / 隔离工作目录 | 失败实验的修改与日志保留；下一次从已知基线开新分支，**不覆写用户当前工作目录** |
| 运行版本指针 | `artifacts/known-good.json` | 只由门禁通过的结果推进；回滚只改指针 |
| 磁盘镜像 | `artifacts/`（F:）与 Data 镜像 | 恢复写入**新的运行目录**，再切换指针；旧目录保留 |

回滚**不得**执行任何形式的「原地覆盖用户工作目录」。恢复动作在 `runs/<restore-id>/` 下准备，校验通过后才切换指针；旧目录、旧指针记录、旧失败证据一律保留。

### D3. 恢复检查点步骤（`unipad restore --checkpoint <id>`）

前置条件（任一不满足即返回对应错误码，不部分执行）：

1. QEMU 未在运行；若在运行，先走 `unipad stop --graceful`（ADR-0002：非优雅终止会破坏 Data 持久化语义）。
2. 检查点存在于 `known-good.json` 的 `current` 或 `history`；不存在 → `CONFIG_INVALID`。
3. `config/sources.lock.json` 的 SHA-256 与检查点记录的 `source_lock_sha256` **一致**；不一致 → `CONFIG_INVALID`（版本基线不同，不属于同一可启动版本）。
4. 该检查点的 `artifact-manifest.json` 可读且其 SHA-256 与记录一致；缺失 → `ARTIFACT_MISSING`，哈希不符 → `IMAGE_HASH_MISMATCH` / `ARTIFACT_MANIFEST_INVALID`。
5. 磁盘空间充足（F: 可用空间检查；C: 0.6GB、E: 0.5GB 视为不可用位置）。

执行顺序（每步落证据，任一步失败即停并保留现场）：

1. 记录**恢复前状态**：当前指针、当前 `runs/` 目录清单、正在使用的镜像路径与哈希。
2. 校验产物：逐项核对 `artifact-manifest.json` 的存在、尺寸非零、SHA-256、架构（PRD §7.4）。
3. 在 `runs/<restore-id>/` 下重建运行目录（不复用、不覆盖旧目录）。
4. **Data 处理分两种显式模式，无默认吞掉用户数据的模式**：
   - `--data keep`（默认）：保留现有 Data 镜像，仅恢复系统侧；用于「系统坏了但用户数据要留下」。
   - `--data snapshot <id>`：恢复到检查点记录的数据快照；**必须显式指定**。
   - 两种模式都在恢复记录中写明实际采用的模式与 Data 镜像哈希。
5. 切换指针：把目标检查点写为 `current`，原 `current` 入 `history`。
6. 启动验证：以恢复后的产物执行一次冷启动验证（`unipad start --profile tcg --artifact <manifest>` → `unipad verify`），通过才把本次恢复记为成功；未通过 → `RESTORE_FAILED`，指针回退到恢复前状态并保留全部证据。

### D4. 保留什么证据

| 证据 | 位置 | 说明 |
| --- | --- | --- |
| 恢复前状态记录 | `runs/<restore-id>/pre-restore.json` | 旧指针、旧目录清单、镜像哈希 |
| 产物校验结果 | `runs/<restore-id>/artifact-verify.json` | 逐项哈希与架构 |
| 恢复记录 | `runs/<restore-id>/restore.json` | 检查点 ID、Data 模式、每步结果、耗时 |
| 恢复后验证 | `runs/<restore-id>/verification.json` | 冷启动与门禁验证结论 |
| 失败现场 | 原目录不动 | 上次失败证据不被覆盖（PRD §5.2 用例 9） |

日志与证据中**不得**出现账号令牌、签名私钥、用户文档内容（PRD §9.2）。

### D5. 命令映射（PRD §9.1）

| 命令 | 作用 |
| --- | --- |
| `unipad stop --graceful` | 回滚/恢复前的优雅停机（QMP 关机 → 等待客体卸载/sync → 结束进程） |
| `unipad restore --checkpoint <id>` | 执行 D3；`--data keep\|snapshot <id>` 必须显式 |
| `unipad start --profile tcg --artifact <manifest>` | 以恢复后的产物启动（显式 TCG，见 ADR-0005） |
| `unipad verify --suite ipa-mvp` | 恢复后的门禁验证 |
| `unipad collect --run <id>` | 汇总本次恢复的证据集合（PRD §9.2） |

命令返回机器可读状态与稳定错误码；失败退出码遵循 `unipad.errors.ExitCode`（FAILURE=1 / USAGE=2 / BLOCKED=3）。

## Consequences

**正面**

- 「已知可启动版本」有唯一指针与可校验哈希，FR10 的两个条件（历史产物 + 磁盘副本）都可被证明。
- 源码分支、版本指针、磁盘镜像三者分离，回滚不会破坏用户正在进行的源码工作或用户 Data。
- 恢复本身也要过验证，避免「回滚到一个其实也启动不了的版本」还被记为成功。

**负面 / 代价**

- 每次恢复都要完整校验哈希并跑一次冷启动验证，耗时不可忽略（冷启动目标是 ≤30 分钟，PRD §5.3）。
- `--data snapshot` 依赖事先存在的数据快照；若不定期制作快照，用户数据只能 `keep` 不能回退。
- 指针追加式历史会增长，需要保留策略（与 PRD §8.3 的 artifact 保留期一致）。

**不做的事**

- 不原地覆盖用户工作目录或用户 Data 镜像。
- 不用未校验的旧包冒充当前构建（PRD 附录 A 禁止事项）。
- 不在门禁未通过时更新 `known-good.json`。
- 不用回滚成功掩盖导致回滚的失败原因；失败签名与证据必须保留（PRD §7.5）。

## Evidence-required

一次回滚要被记为成功，必须在 `runs/<restore-id>/` 留下（缺一即 `EVIDENCE_INCOMPLETE`）：

1. `pre-restore.json`：恢复前指针、目录清单、镜像哈希。
2. `artifact-verify.json`：产物逐项存在/尺寸/SHA-256/架构校验结果。
3. `restore.json`：检查点 ID、采用的 `--data` 模式、每步结果与耗时、指针切换前后值。
4. `verification.json`：恢复后冷启动与门禁验证结论（含实际 `-accel tcg` 参数与 QEMU 版本）。
5. 未覆盖的旧证据：恢复前的失败现场与上一次失败记录仍在原位（PRD §5.2 用例 9）。

只有 `verification.json` 判通过，`known-good.json` 的指针切换才生效；否则指针回退并按 `RESTORE_FAILED` 记录。

## Status

accepted (code stage)。代码阶段可完成：`known-good.json` 读写与校验、恢复前置条件检查与错误码映射、`runs/<restore-id>/` 隔离恢复目录、Data 模式开关、恢复记录与 `restore.json` 输出。**真机恢复（FR10 现场验收）未执行，标记 UNVERIFIED**；「恢复后确能冷启动」依赖现场产物与门禁。
