# ADR-0007: 安装门禁条件只认「成功的终态」，不认「终态」

- 编号：ADR-0007
- 日期：2026-10-23
- 状态：accepted (code stage) —— 代码阶段已采纳；真机现场验证 **UNVERIFIED**
- 关联需求：PRD §7.4（验证器依据固定条件判定；验证逻辑修改独立记录）、§9.1（安装返回契约）、§6 M10/M10a/M11/M12 与 `ipa-mvp` 门禁
- 关联契约：`unipad.controller.verifier` 的 `install.stage_terminal` 条件、`unipad.ipa.faults.SUCCESS_STAGES` / `is_success`、`unipad.models.InstallResult.SUCCESS_STAGES`

## Context

`InstallResult.TERMINAL_STAGES` 是 `("installed", "registered", "failed")`——它回答的是「安装链是否已经结束」，而不是「安装是否成功」。`install.stage_terminal` 条件此前的实现是：

```python
if stage not in InstallResult.TERMINAL_STAGES:
    return [f"install_stage_not_terminal:{stage}"]
```

于是 `stage == "failed"` 满足该条件。它之所以没有直接变成事故，是因为同一组里的 `install.error_free` 会拒绝任何带 `error_code` 的事实，而 `src/unipad/ipa/installer.py` 今天每一处写 `InstallStage.FAILED` 的位置都紧跟一个 `error_code` 赋值（installer.py:323/348/354/372/394/400/406/421，以及 cli.py:1015、cli.py:1121、guest/client.py:353）。

这个「今天不会发生」不能当作保证，理由有三：

1. **验证器读的是磁盘上的 `install.json`**（`verifier.load_facts`）。它对本文件之外的任何人都必须成立：被截断、被手改、或由未来某个新生产者写出的 `install.json` 完全可以是 `{"stage": "failed", "error_code": null}`。
2. **验证器是最后一道防线**。PRD §7.4「模型提交门禁证据，验证器依据固定条件判定」的要点正是判定不能依赖被判定方自己的自我约束。让一个条件靠兄弟条件兜底，等价于把两条规则写成一条。
3. **条件名与实现不一致**。描述写「install reached a terminal stage (copy is not install)」，实际放行了失败；读者会以为这里已经挡住了失败。

## Decision

1. **拆开两个词表**：在 `unipad/ipa/faults.py` 新增 `SUCCESS_STAGES = ("installed", "registered")` 与 `is_success()`，与既有 `TERMINAL_STAGES` / `is_terminal()` 并列；`InstallResult` 同步持有同名 `SUCCESS_STAGES`（既有测试已断言两者一致）。`TERMINAL_STAGES` 的既有含义与取值**不变**，`is_terminal` 的行为不变。
2. **`install.stage_terminal` 改判成功终态**：条件改为 `stage not in InstallResult.SUCCESS_STAGES`。失败原因串保持 `install_stage_not_terminal:<stage>` 不变（该字符串此前只在本模块内出现，未进入对外契约；保持不动可避免既有记录与测试的语义漂移）。
3. **条件描述改写**为「install reached a terminal successful stage -- neither a copy nor a failure counts」，与实现一致。
4. **`VERIFIER_VERSION` 1.0.0 → 1.1.0**。本改动是**收紧**判定，不是放宽；按模块自身约定（任何条件逻辑变更都要升版本并在此记录），升 minor 并在本 ADR 留下理由，使历史门禁结果仍可回溯到产生它的那套逻辑。
5. **测试先行**：先加 `test_a_failed_install_without_an_error_code_still_fails_the_gate`，确认它在旧实现下**真的通过**（`passed=True`，failure_reasons 为空），再改实现。同时补 `is_success` / `SUCCESS_STAGES` 的单测。

## Consequences

**正面**

- 一个 `stage="failed"` 的事实文件，无论有没有 `error_code`，都不可能让 `M10` / `M10a` / `M11` / `M12` / `ipa-mvp` 通过。三个安装条件各自独立成立，不再互相依赖。
- `is_terminal` 与 `is_success` 的分开使「是否结束」和「是否成功」在调用点显式可选，后续代码不能因为用错一个而意外放宽门禁。
- 门禁结果带着 `verifier_id@1.1.0`，与本 ADR 一一对应。

**代价 / 负面**

- 任何依赖「failed 也算 terminal 所以条件通过」的旧行为都会失败。已全量检索：`src/` 与 `tests/` 中没有这样的调用点（`is_terminal` 的现有使用者只用于判断链是否结束）。
- 多了一个词表，需要保持 `SUCCESS_STAGES` 与 `InstallResult.SUCCESS_STAGES` 同步；已用单测固定（`test_success_stages_exclude_the_failure_that_is_also_terminal`）。

**未解决 / 现场项**

- 本次修复只在 Windows 代码阶段用合成 `install.json` 验证。真机（Ubuntu + 真 iPadOS 客体）上的安装门禁表现 **UNVERIFIED**，需要在现场阶段用真实安装成功/失败各跑一次才能宣称。
