# Windows 启动器（UniPad Launcher）

- 文档：`docs/launcher.md`
- 日期：2026-10-23
- 状态：code stage complete
- 关联需求：PRD §3.2 P1 第 6 条「简洁的 Windows 启动器：开始、停止、导入 IPA、查看日志、恢复。」
- 关联契约：`python -m unipad`（PRD §9.1 命令集）、`unipad.errors.ExitCode`（0/1/2/3）、`unipad.host.qemu.NON_ASCII_PATH_REASON`
- 交付物：`scripts/windows/UniPad-Launcher.ps1`（主）、`scripts/launcher.cmd`（双击 shim）、`tests/acceptance/test_launcher_script.py`

> **本次修订（2026-10-23，晚于初版）**：初版把启动器放在 `scripts/launcher.ps1` 并自行实现
> `start`。盘点后发现 `scripts/windows/Start-UniPad.ps1` 已经拥有 `start` 的 TCG 前置守卫
> （非 TCG 直接以 `accelerator_not_tcg` 退出）、解释器解析、UTF-8 流解码、direct-module
> 回退和三向 `Get-ControllerVerdict` 判定。启动器因此**搬迁到 `scripts/windows/` 并改为委派**，
> 不再重演任何一份逻辑；`scripts/launcher.ps1` 已删除。风格（Verb-UniPadNoun 辅助函数、
> `Write-StructuredError` 形状、退出码映射）逐条对齐既有脚本，读起来像同一个人写的。

> **第二次修订（2026-10-23，晚）**：`logs` 的两种调用方式（带/不带 `-Follow`）现在共享同一个
> 尾部长限常量 `$script:LogTailLines`，并在 stderr 上声明截断——无 `-Follow` 时**不再**打印整份
> 串口日志。外层 catch 的错误码从 `blocked`（映射到退出码 3）改为 `internal_error`（退出码 1），
> 且退出码由 `Get-ExitCodeForErrorCode` 从错误码推导而非并排手写，二者不可能再互相矛盾；同时只
> 报告异常**类型**、绝不原文转述 .NET 消息——`-Follow` 的 catch 曾转述，实测会在本机把一段
> ANSI 代码页的中文本地化消息以乱码形式送到操作者 stderr 上，现已对齐。`-Run` 之外，
> `-ExperimentId` 也进入非 ASCII 预检。本节末尾记录两件本机**无法证明**或**不归本交付物所有**的事。

## 用法

```text
scripts\launcher.cmd <Action> [参数]        # 双击 / 命令行
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\UniPad-Launcher.ps1 -Action <Action> [参数]
```

单个 `-Action` 参数，取值与所需参数：

| Action   | 额外参数                                   | 去向                                                                       |
| :------- | :----------------------------------------- | :------------------------------------------------------------------------- |
| `start`  | `-Artifact <manifest>`（必填）`-Profile` `-ExperimentId` | **委派** `scripts\windows\Start-UniPad.ps1`（TCG 守卫与 verdict 归它所有） |
| `stop`   | `-Graceful` \| `-Force`（二选一）`-Run <id>` | `python -m unipad.controller stop [--graceful\|--force] [--run <id>]`      |
| `install`| `-Ipa <path>`（必填）`-ExperimentId`        | `python -m unipad.controller install <ipa> [--experiment <id>]`             |
| `logs`   | `-Run <id>`（缺省取最新）`-Follow`          | 直接读取 `runs/<run>/serial.log`（不经过 CLI）                              |
| `restore`| `-Checkpoint <id>`（必填）`-Run <id>`       | `python -m unipad.controller restore --checkpoint <id> [--run <id>]`       |

全局开关：`-WhatIf`（只打印解析后的计划 JSON，不启动任何东西）、`-RunsRoot`、`-ArtifactsRoot`。
解释器由 `$env:UNIPAD_PYTHON` 覆盖，否则探测 `python` 再 `py`。

> `-Checkpoint`、`-Run` 与 `-ExperimentId` 都是**标识符**：控制器自己把它们拼到
> `--artifacts-root` / `--runs-root` 之下（`store.dir_for(args.run)`、
> `artifacts_root / args.checkpoint`、`<runs-root>/<experiment-id>`），所以启动器原样传递、
> 不做路径拼接——也因此三者都要过非 ASCII 预检。

```text
# 干跑（验收测试即断言此输出，绝不启动 QEMU）
scripts\launcher.cmd start -Artifact artifacts\manifest.json -WhatIf

# 查看串口日志（只显示最后 100 行，截断量在 stderr 声明）
scripts\launcher.cmd logs -Run exp-1

# 跟踪串口日志，类似 tail -f，Ctrl-C 干净退出
scripts\launcher.cmd logs -Run exp-1 -Follow

# 安装 IPA
scripts\launcher.cmd install -Ipa C:\ascii-only\App.ipa
```

## 退出码契约（与 CLI 一致，原样透传）

- `0` 成功。
- `1` 失败：CLI 返回的失败；`logs` 找不到串口日志、或串口日志路径存在但读不了（目录、
  权限变更、崩溃残留）——两者都是 `artifact_missing`；另外若 CLI 退出 0 但 JSON 判定为
  `failure`，启动器按失败退出（**绝不用成功去消解矛盾载荷**）。
- `2` 用法错误：`-Action` 非法、缺少必填参数、参数与动作不匹配、`stop` 同时给了
  `-Graceful` 与 `-Force`、路径含非 ASCII。**在启动器侧判定，早于任何子进程。**
- `3` blocked：CLI 原样返回（缺物料/预算/无启动记录等）；`logs` 找不到任何 run 也归此类。

stdout 契约按动作区分：只有四个走控制器的动作 **start/stop/install/restore** 保证 stdout 是
**单个 JSON 对象**（控制器的信封原样透传，`Write-Note` 一律写 stderr，可以被管道直接解析）；
`logs` 的 stdout 是串口日志的尾片段，**不是 JSON**——它不经过 CLI，直接读盘。

启动器不吞失败，退出码原样返回。`start` 的退出码来自被委派的 `Start-UniPad.ps1` 子进程。

## 关键设计

- **仓库根解析**：取自脚本自身位置 `$PSScriptRoot` 的父目录（脚本在 `scripts/windows/`），
  与操作者从何处双击无关；控制器以仓库根为工作目录运行，因此 `runs/`、`artifacts/`、
  `config/*` 的相对默认值始终正确。`--runs-root` / `--artifacts-root` 显式传入，
  `logs` 与 CLI 永远一致。
- **非 ASCII 预检（本机最重要的健壮性特性）**：本机用户目录 `C:\Users\<账户名>`
  含非 ASCII，Windows QEMU 构建用 ANSI 代码页解析路径，会在约 30 秒后以难以理解的
  `open <path> failed` 失败。启动器在调用解释器/QEMU **之前**检查仓库根、runs/artifacts
  根、以及操作者提供的 artifact/IPA/checkpoint/**run 标识符**/**experiment 标识符**/串口日志路径；
  命中即给出可操作信息并以退出码 2 中止。（`-Run`/`-Checkpoint`/`-ExperimentId` 都是控制器会
  拼接成路径的标识符，因此与显式路径同等对待。）
- **委派而非重演**：`start` 以子进程方式运行 `Start-UniPad.ps1`，透传其 stdout/stderr/退出码。
  TCG 守卫（非 TCG → `accelerator_not_tcg`）、`-Profile` 语义、控制器 verdict 三向判定都
  只有一份实现。
- **Python 选择**：`$env:UNIPAD_PYTHON` 优先；否则依次探测 `python` 再 `py`（本机无
  `python3`），且每个候选都必须**真的能 `import unipad`**。本机 `python`=3.12.9（有 shim），
  `py`=3.14（无 shim，会被跳过）——只看「PATH 上是否存在」是不够的，那样会在 30 秒后以
  难以理解的 `No module named unipad` 失败。报告实际使用的解释器；全都不可用则以退出码 3
  明确报 `blocked`，绝不猜测。
- **logs**：`-Follow` 用 `Get-Content -Wait` 实现 `tail -f`；日志尚不存在时先等待再跟踪；
  Ctrl-C 视为干净停止（退出码 0）。两种调用方式共享**同一个**尾部长限常量
  `$script:LogTailLines = 100`，因此无 `-Follow` 时也只显示最后 100 行——它曾经是无界读取，
  把整份访客串口日志倒在 stdout 上，而同一个动词的 `-Follow` 却又截尾，操作者无法判断自己
  在看哪一种；访客串口日志随访客运行持续增长，启动器是操作者的*视图*，不是消防栓。截断在
  **stderr** 明确声明（总行数、总字节、完整日志路径），不让操作者以为这就是全部；行数用流式
  读取统计，声明本身不破坏尾界限。`evidence.json` 的位置顺带提示。
- **失败分类与退出码同源**：`Get-ExitCodeForErrorCode` 是 `unipad.errors._CODE_TO_EXIT` 的
  镜像。启动器发出的每个错误码都必须能在它上面解析，退出码一律**从错误码推导**，绝不与手挑的
  数字并排书写——外层 catch 曾经报 `blocked`（映射 3）却退 1，按码分支的调用方与按退出码分支
  的调用方对同一次运行得出相反结论。现在两者不可能再漂移。

## 四字段状态（代码阶段与现场阶段分离）

| 阶段 | 状态 |
| :--- | :--- |
| 实现状态 | CODE COMPLETE：`scripts/windows/UniPad-Launcher.ps1`（811 行：493 行文档字符串/参数/辅助函数 + 318 行主流程）、`scripts/launcher.cmd`（8 行 shim）、`docs/launcher.md`。 |
| Windows/模拟测试状态 | SIMULATION PASS：`tests/acceptance/test_launcher_script.py` 26 个测试函数（pytest 收集 32 项，含参数化展开）全部通过；覆盖退出码 2 契约、非 ASCII 拒识（artifact/IPA/run 标识符/experiment 标识符）、跨工作目录仓库根解析、`-WhatIf` 计划 JSON（含 `delegates_to`）、解释器上报（含「能解析但不能 import 的解释器必须跳过」）、logs 读取/缺失/不可读、logs 有界尾部长限与两种调用方式一致、外层 catch 的码/退出码一致、`-Follow` 只报异常类型不转述原始消息，以及两条**真实调用**用例（对空 runs root 执行 `stop`、对缺失 IPA 执行 `install`，均在到达客体前被阻断/失败）。无测试启动真实 QEMU。 |
| 现场验证状态 | UNVERIFIED：未在操作者本机以真实 IPA/固件跑通 `start`/`install`/`restore`；现场=用户 Windows 本机启动真实 iPadOS，代码阶段无法取得该证据。`logs -Follow` 的 Ctrl-C 异常类型同样未证明（见下）。 |
| 阻断原因 | 无 QEMU AArch64 固件产物、无 IPSW、无客体安装服务；`py` 解释器版本不匹配（3.14 无 unipad shim）为探测逻辑已规避的环境事实。 |

## 测试

`tests/acceptance/test_launcher_script.py`（`pytestmark = acceptance`）以子进程启动**真实**的
`UniPad-Launcher.ps1`，不断言任何重实现：用法错误→退出码 2；非 ASCII artifact/IPA/run 标识符/
experiment 标识符被拒；从异工作目录解析仓库根；`-WhatIf` 输出单个 JSON 计划（`start` 的计划必须
声明 `delegates_to = Start-UniPad.ps1`）；解释器上报（含用 `py.exe` 副本顶替 `python` 的阴影
测试，断言「能解析但不能 import 的解释器必须跳过」→ 退出码 3）；logs 读取/缺失/不可读。

其中两条用例**真实调用控制器**而非只看 `-WhatIf` 计划，这是承重测试（对质）逼出来的：
`-WhatIf` 打印的是启动器*打算*执行的 argv，与真正交给 CLI 的 argv 不是同一个向量。曾有一个真实
bug——启动器把已含 `-m unipad.controller` 的向量又交给会自行补齐 `-m` 的
`Invoke-UniPadController`，导致 CLI 看到 `python -m unipad.controller -m unipad.controller stop`
并以 `command: unipad.controller`、退出码 2 失败；**计划输出全程看起来都是对的**。现在
`test_real_stop_reports_the_stop_command` 与 `test_real_install_reports_the_install_command`
断言真实信封里的 `command` 字段，该 bug 会被这两条抓住。二者分别以「空 runs root → blocked」
和「缺失 IPA → artifact_missing」收场，均不触及客体。

`logs` 的有界尾部长限由两条用例钉住：`test_logs_without_follow_is_bounded_to_the_same_tail`
（400 行日志只能看到尾部，且总行数与完整路径必须在 stderr 声明）与
`test_logs_follow_and_plain_read_agree_on_the_tail_size`（两种调用方式显示的行数必须逐行相同——
钉的是*成因*：尾界限曾分别字面书写在两处，于是一处漂移）。外层 catch 由
`test_unexpected_error_code_and_exit_code_agree` 钉住：错误码必须是 `internal_error`、退出码
必须是 1、且只允许携带异常**类型**（`|` 让 `GetFullPath` 在任何子进程之前抛错，是唯一确定性的
触发路径）。`-Follow` 的 catch 由
`test_logs_follow_reports_the_exception_type_without_the_raw_message` 钉住：用
`msvcrt.locking` 锁住串口日志的字节区间，使 `Get-Content -Wait` 抛
`System.IO.IOException`——这是唯一能确定性地走进那个 catch 的办法——然后断言 stderr 只有异常
**类型**、没有任何不可解码的字节。

本文档自身的三个数字（启动器行数、测试函数数、pytest 收集数）由
`test_docs_numbers_are_derived_from_the_tree` 从工作树现场推导，另有两条规定本文档必须如何描述
`logs` 的有界性与 stdout 的 JSON 契约——文档是证据，写错的数字和写错的行为描述都是错误的主张。

## 未证明项：`logs -Follow` 的 Ctrl-C 异常类型

`-Follow` 的 catch 是**泛型**的：它不判断异常类型，只把 `$_.Exception.GetType().FullName` 写到
stderr（`follow ended: <类型>`），Ctrl-C 走退出码 0 的干净停止路径。它**不**转述异常的原始
消息——这条线与外层 catch 完全一致，理由也相同：.NET 消息会引用出错的值（工作目录非 ASCII 的主机上
等于把 Windows 用户名写到 stderr，PRD 9.2），且在本机它是一条 ANSI 代码页的中文本地化字符串。
实测：用 `msvcrt.locking` 锁住串口日志的字节区间（这是唯一能确定性地走进这个 catch 的办法），
`Get-Content -Wait` 抛 `System.IO.IOException`，而它的消息原样转述到 stderr 就是一段无法解码的
GBK 乱码。类型名已经足够区分「干净停止」和「读不了」，原始消息只会带来乱码和泄露。

退出码保持 0 是刻意的：Ctrl-C 必须是 0，而本机无法按类型把 Ctrl-C 和 IOException 分开（见下），
按类型分支就是猜测。stderr 那一行因此是唯一的信号。

**本机无法证明 Windows PowerShell 5.1 在 Ctrl-C 时抛出的异常类型**，因此刻意不为一个没有观察到的
类型写分支。已测量的事实：从一个 stdio 被重定向的父进程向子进程发送
`GenerateConsoleCtrlEvent(CTRL_C_EVENT, pid)` 返回成功但子进程毫无反应——它继续运行、不抛异常、
不退出。原因是该父进程根本没有控制台（`GetConsoleWindow()` 为 0），事件无处投递；换
`CREATE_NEW_PROCESS_GROUP` 指向子进程自己的进程组，结果相同。也就是说：**自动化测试无法把真的
控制台 Ctrl-C 投递给这个子进程**，异常类型因而不可观测。

什么能定案：操作者在真实控制台（`cmd.exe` 或 PowerShell 窗口）里运行
`scripts\launcher.cmd logs -Run <id> -Follow`，然后按 Ctrl-C。stderr 上的
`follow ended: <类型>` 一行就是启动器自带的探针——它不分支、不猜测，第一次真的 Ctrl-C 会把类型
记录下来。在那之前，任何「Ctrl-C 抛 X」的写法都是编造。

## 已关闭：`start` 的 verdict 曾在另一份脚本里失效（调用链审查 7.3）

这一段记录一个已经修掉的缺陷，因为它是本交付物唯一一处「两份拷贝已经分叉」的实例，而分叉本身
才是要防的类别。

`scripts/windows/Start-UniPad.ps1` 的 `Get-ControllerVerdict` 曾读取 `$names` 却从未在该函数内
赋值（`UniPad-Launcher.ps1` 的同名函数有这一行）。PowerShell 没有块作用域，被调用函数里的赋值
不会泄漏回调用方，因此 `$names` 恒为 `$null`，两个 `foreach` 永不命中，verdict 恒为
`indeterminate`。当时实测（抽取两个脚本的同名函数、喂同一个成功载荷）：

```
Start-UniPad.ps1       verdict = indeterminate
UniPad-Launcher.ps1    verdict = success
```

后果是**安全但残缺**的：`indeterminate` 分支必然命中，打印警告并以 0 退出，其后的成功分支成为
死代码。启动器从不谎报成功（符合 PRD 7.4），但 `scripts\launcher.cmd start` 永远无法确认一次
成功的启动。

修法是在 `Test-PayloadError` 调用之后补 `$names = @($Payload.PSObject.Properties.Name)` 一行。
该行已补齐，`Start-UniPad.ps1` 现在能对成功载荷报告 `success`。

钉住它的是 `tests/acceptance/test_start_script_verdict.py`：它从两个真实脚本中各抽取出同名函数
（绝不 dot-source，因为这些脚本会启动客体），用同一组 12 个载荷驱动两份拷贝，断言它们**达成同一
答案且该答案正确**——共享一个错误答案不能靠"共享"过关。其中一条专门断言每个应为 `success` 的载
荷确实读到 `success`，那正是缺 `$names` 时的失效形状。启动器刻意不重演一份 verdict，因为重演正是
调用链审查 §7.4 点名批评的重复实现；测试断言一致性，而不是第三份实现。
