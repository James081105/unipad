# 测试策略（Testing Strategy）

- 文档：`docs/testing-strategy.md`
- 日期：2026-10-22
- 状态：accepted (code stage)
- 关联需求：PRD §5.3、§6 全部门禁、§7.4 验证器要求、§7.5 实验预算、§9.2 运行证据最小集合、§13
- 关联契约：`unipad.models.EvidenceSet` / `GateResult` / `ExperimentRecord`、`unipad.errors.ErrorCode.{EVIDENCE_INCOMPLETE, VERIFICATION_FAILED, BUDGET_EXCEEDED}`、`pyproject.toml` pytest markers（`slow` / `acceptance`）、`docs/implementation-plan.md`

## Context

1. 项目 CLAUDE.md「代码阶段与现场阶段分离」要求：开发环境当前为 Windows，必须先在本机完成 PRD 中所有**可编码阶段**（文档与设计、全部代码、测试、模拟测试、审计和回滚材料），再进入现场部署；不得因为没有 Ubuntu、PostgreSQL/ClickHouse/NAS 实例或希沃一体机而停止代码工作，应使用 Windows 可运行的 fake/in-memory/test harness、协议模拟、故障注入、浏览器自动化和交叉编译完成可重复验证。
2. 同一节要求每个阶段**分别记录四个字段**：实现状态、Windows/模拟测试状态、现场验证状态、阻断原因；且 **CODE COMPLETE 或 SIMULATION PASS 不得写成现场 PASS**；缺少现场环境只能标记 UNVERIFIED/BLOCKED。
3. UniPad 的现场=用户 Windows 本机启动真实 iPadOS（PRD §1：最终运行和验收必须发生在这台 Intel Windows 电脑上）。代码阶段**不可能**获得该证据：没有 QEMU AArch64 固件产物、没有 IPSW、没有客体安装服务。
4. 因此代码阶段的测试必须回答两类不同的问题：(a) 我们的宿主侧逻辑是否正确；(b) 当现场条件具备时，验证器是否会因为证据不足而拒绝通过。二者都不需要真实硬件。
5. PRD §7.4 已规定验证器的独立性：模型提交门禁证据，验证器依据固定条件判定；**验证逻辑的修改独立记录，不能随功能修改偷偷放宽**。

## Decision

### D1. 四字段记录约定（每个阶段强制）

每个阶段/WP 在 `docs/implementation-plan.md` 与最终报告中都必须按下列四字段分别记录，**不得互相冒充**：

| 字段 | 允许取值 | 含义 |
| --- | --- | --- |
| 实现状态 | `CODE COMPLETE` / `PARTIAL` / `NOT STARTED` / `UNVERIFIED` | 代码是否写完并有对应测试 |
| Windows/模拟测试状态 | `SIMULATION PASS` / `FAIL` / `NOT RUN` | 在本机用 fake/harness 跑的结果 |
| 现场验证状态 | `PASS` / `UNVERIFIED` / `BLOCKED` | 用户本机真实 iPadOS 运行结果 |
| 阻断原因 | 具体项或 `—` | 现场被什么卡住（缺素材/缺凭证/缺预算/缺环境） |

硬规则：

- **`CODE COMPLETE` ≠ 现场 `PASS`；`SIMULATION PASS` ≠ 现场 `PASS`。**
- 现场未执行前一律 `UNVERIFIED` 或 `BLOCKED`，并在最终报告**列明现场未验证项清单**。
- 不得以现场缺失为由宣称代码未完成，也不得把现场部署/试点/扩容/运维冒充为已完成。
- 字段值变更必须能指向证据（提交、测试输出、运行记录）；不能只改文字。

### D2. 什么算证据（什么不算）

算证据（PRD §9.2 最小集合 + §7.4）：

- `experiment.json`（PRD §7.3 格式，含可证伪假设、`base_commit`、`source_lock_sha256`）
- `host-inventory.json`、`artifact-manifest.json`（逐项 SHA-256 与架构）
- 实际 QEMU 参数、固件/串口日志、可获取的客体内核/进程日志、QMP/控制日志
- 带时间戳截图、`verification.json`；GPU 开启时另存宿主桥接日志
- 测试运行输出（pytest 退出码 + 失败详情），保存为文件而非口头转述

不算证据（出现即判未验证）：

- 模型自称完成、无证据的「成功」文字
- 串口单行文本单独证明阶段成功（PRD §7.4：串口文本用于定位）
- README 可读、脚本返回 0、仅下载二进制（PRD §6 M0「不足以通过的情况」）
- 只运行出 QEMU 窗口、只出现静态图片、只展示主屏幕截图
- 把 mockfs 中 PID 1 测试程序当成系统 launchd（PRD §6 M8、附录 A）
- 把预装图标或静态画面当成 IPA 运行（PRD 附录 A）
- 旧包冒充当前构建；`EvidenceSet.check_complete()` 非空时判 PASS

证据缺失 → `EVIDENCE_INCOMPLETE`；固定条件不满足 → `VERIFICATION_FAILED`。两者都不是「成功」。

### D3. 基于 fake 的模拟测试方法（代码阶段主力）

目标：在不具备真实固件/IPSW/客体的情况下，让宿主侧全部逻辑可重复地跑起来，并让验证器的「拒绝能力」被测试。

| Fake | 位置 | 替代的真实依赖 | 必须能注入的故障 |
| --- | --- | --- | --- |
| Fake QEMU | `tests/fakes/` | `qemu-system-aarch64` 进程 | 启动失败（`qemu_launch_failed`）、无进展超时（`qemu_timeout`）、非零退出、参数缺项 |
| Fake QMP server | `tests/fakes/` | TCP 127.0.0.1 上的 QMP 服务 | 端口 never ready（`qmp_unavailable`）、协议错误（`qmp_protocol_error`）、关机不响应、能力协商失败 |
| Fake guest service | `src/unipad/guest/` + `tests/fakes/` | 客体安装/传输服务 | 传输哈希不符（`transfer_failed`）、安装服务不可用（`install_service_unavailable`）、注册失败（`registration_failed`）、策略拒绝（`signature_or_policy_rejected`）、依赖缺失（`missing_dependency`）、dyld 失败（`dyld_failed`） |
| Fake 镜像/产物 | `tests/fixtures/` | IPSW/RootFS/Data 镜像、artifact-manifest | 哈希不符（`image_hash_mismatch`）、清单无效（`artifact_manifest_invalid`）、产物缺失（`artifact_missing`） |

规则：

- Fake 必须**确定性**：同一输入同一输出；禁止在 fake 里放真实时钟/随机数导致的偶发失败（PRD 箴言 58：随机失败往往是并发问题）。
- Fake 的失败模式必须覆盖 PRD §10 的完整故障分类与 PRD §6 各门禁的「不足以通过的情况」。
- 模拟验收测试打 `acceptance` marker（`pyproject.toml` 已定义），长时间的打 `slow`；CI 与本地都能重复运行。
- 模拟通过只写 `SIMULATION PASS`，且必须附测试输出证据。
- Fake 不得被当成「已经支持该能力」的依据；Fake 存在不等于客体支持。

### D4. 对质（承重/变异测试）约定——审计阶段

依据项目 CLAUDE.md「五个审查」：对质即**承重测试**——改掉代码语义，观察对应点的单元测试是否通过；**如果过了，说明那是坏代码（测试没有承重）**。

执行约定：

1. 在对质目标处做一个**语义反转/删除/弱化**的小改动（例如：把只读断言改成恒真、把阶段推进跳过校验、把哈希比对改成恒等、把优雅关机等待改成立即返回）。
2. 运行对应点的单元测试：
   - **测试失败** → 该处测试有承重，恢复改动。
   - **测试仍通过** → 该处测试是坏代码：必须补强测试（补断言/补用例/补故障注入），再重做对质，直到测试失败为止。
3. 对质范围至少覆盖：错误码映射与退出码、安装阶段语义（传输成功不得报 `installed`）、加速器守卫（非 `tcg` 必须拒绝）、证据完整性检查、已知可启动指针校验、Data 只读/可写断言。
4. 对质 agent **至多自主运行四个小时**；超时必须由主 agent 介入或转人工介入，不得无限续跑。
5. 每次对质记录：目标位置、改动内容、测试结果、结论（有承重/坏代码）、修复方式；改动本身不得留在代码里（对质是审计手段，不是功能修改）。
6. 对质结论进入 R5 五维审计记录（反向 / 性能 / 规范 / 调用链 / 对质），作为最终审计验收报告的输入。

### D5. 验证器的独立性

- 验证逻辑与功能代码分开维护；修改验证条件必须独立记录理由与影响（PRD §7.4），不得随功能修改偷偷放宽。
- 同一失败签名连续 3 次且无新增证据时停止该路径（PRD §7.5）；每个假设最多 3 次受控实验；预算超限返回 `BUDGET_EXCEEDED` 并停止付费路径，而非静默继续。
- 调试运行与正式计时分开：断点暂停、观察工具延迟、宿主挂起不得混入性能结果（PRD §7.4）。

## Consequences

**正面**

- 代码阶段可以在 Windows 上全部完成并有可重复证据，现场缺失不再阻断代码 Goal。
- 验证器的「拒绝能力」被 fake 与对质测试覆盖，降低现场阶段「无证据报成功」的风险。
- 四字段让最终报告的现场未验证项清单成为机械产出，而非事后回忆。

**负面 / 代价**

- 需要维护一批 fake 与夹具，本身是持续成本；fake 与真实行为的偏差要靠现场验证暴露。
- 对质会拉长审计阶段时间，且有 4 小时自主上限，需要主 agent 排期。
- `SIMULATION PASS` 的数量会远多于现场 `PASS`，报告可读性下降——这是必须承受的代价，不能用合并字段换整洁。

**不做的事**

- 不把模拟结果写成现场结果，不用 fake 证据充当硬件证据。
- 不为让测试通过而放宽验证条件或删除验收用例（PRD 文档变更规则）。
- 不用覆盖率数字替代状态覆盖（PRD 箴言 94：测试状态覆盖，而非代码覆盖）。

## Evidence-required

每个阶段的四字段都要有可指向的证据，缺证据的字段只能写 `UNVERIFIED` / `NOT RUN`：

| 字段 | 最低证据 |
| --- | --- |
| 实现状态 | 变更文件清单与提交记录；对应测试文件存在 |
| Windows/模拟测试状态 | pytest 运行输出（退出码 + 失败详情，存为文件；`acceptance` / `slow` marker 分别统计） |
| 现场验证状态 | PRD §9.2 最小证据集合：`experiment.json`、`host-inventory.json`、`artifact-manifest.json`、实际 QEMU 参数、固件/串口日志、客体日志、QMP 日志、带时间戳截图、`verification.json`；GPU 开启时另存宿主桥接日志 |
| 阻断原因 | 具体缺失项（素材/凭证/预算/环境）与对应 `unipad.errors` 错误码 |

审计阶段另需：五维审计记录（反向 / 性能 / 规范 / 调用链 / 对质）与对质逐条结论（目标位置、改动内容、测试结果、结论、修复方式）。

## Status

accepted (code stage)。本节约定的四字段表在 `docs/implementation-plan.md` 已首建；fake 与模拟验收套件属 R3 工作包。**全部现场验证状态当前为 `UNVERIFIED` / `BLOCKED`**，阻断原因逐项列于该表（无 QEMU 二进制、无固件产物、无 IPSW、无真实客体安装服务、Actions 预算未授权）。
