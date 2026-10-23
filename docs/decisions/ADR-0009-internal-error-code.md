# ADR-0009: `internal_error` 是封闭错误分类表之外的一个有记录的决定；以及 18 个 PowerShell 助手名跨 5 脚本重复的立项评估

- 编号：ADR-0009
- 日期：2026-10-23
- 状态：accepted (code stage) —— 第一部分的决定已采纳（代码早已如此运行，本 ADR 补上它自称必需的那份决策记录）；其中**一条保证当前未交付**，修复已按属主规则移交，见 G4。第二部分是**评估，不是决定**：它不为重构背书，只为重构属主提供可立项的事实。真机现场验证 **UNVERIFIED**
- 关联需求：PRD §10（错误码必须来自 `src/unipad/errors.py` 的封闭集）、§9.1（每条命令在 stdout 上恰好打印一个 JSON 对象；固定退出码）、§9.2（日志不含账号、令牌、签名私钥和用户文档）、CLAUDE.md #38 Crash Early / #39 Use Assertions to Prevent the Impossible / #15 DRY
- 关联契约：`unipad.controller.cli.INTERNAL_ERROR_CODE`（`cli.py:87-90`）、`unipad.errors.ErrorCode` / `_CODE_TO_EXIT` / `UniPadError.exit_code`（`errors.py:17-96`）、`scripts/windows/UniPad-Launcher.ps1` 的 `Get-ExitCodeForErrorCode`（`:251-275`）

---

# 第一部分：`internal_error` 的决定（规范审查 CG-4）

## Context

规范审查 Q4 逐条核对了五个范围内模块用到的每一个 `ErrorCode.*`，结论是全部落在封闭集内，只列了两个例外，本 ADR 处理第一个：

> **CG-4** —— `internal_error` is outside the §10 closed set and has no decision record.
> Clause: PRD §10; `errors.py:5-6` "renaming or repurposing a code requires a decision record (docs/decisions/)".
> File: `src/unipad/controller/cli.py:87-90`. `docs/decisions/` holds ADR-0001 … ADR-0008; none covers `internal_error`.
> The code self-documents the requirement ("Promoting it to `errors.ErrorCode` requires a decision record") and the mitigation is real — `main` catches `UniPadError` *before* the catch-all, so it cannot mask a known fault — but the stated precondition is unmet.

`docs/decisions/` 在 Round A 之后持有 ADR-0001 … ADR-0008，确实没有一条覆盖它。代码自己写下的前置条件（`cli.py:87-89`）：

```python
#: Not part of the PRD 10 fault taxonomy: a controller-side catch-all for
#: unexpected exceptions.  Promoting it to ``errors.ErrorCode`` requires a
#: decision record (docs/decisions/); it must never mask a known fault.
INTERNAL_ERROR_CODE = "internal_error"
```

它是一个**字符串常量**，不是 `ErrorCode` 成员，因此不在 `errors._CODE_TO_EXIT`（`errors.py:71-77`）里，`UniPadError.exit_code`（`:95-96`）对它的回答是 `default → ExitCode.FAILURE`，即退出码 1。

它有两个生产发射点，都在 `main` 里：

| 位置 | 触发 | 信封 |
| --- | --- | --- |
| `cli.py:2767-2768` | `KeyboardInterrupt` | `command="<interrupted>"`, `message="interrupted"`, 无 `exception_type` |
| `cli.py:2769-2776` | 任何未被前面处理器认领的 `Exception` | `command=<命令名>`, `message="unexpected controller error: <类型>: <str(exc)>"`, `details.exception_type=<类型>` |

并且它**跨两个实现**存在：PowerShell 启动器的外层 catch 也发同一个码（`UniPad-Launcher.ps1:272` `'internal_error' { return $script:ExitFailure }`），`tests/acceptance/test_launcher_script.py:427-435` 用字面量钉住「码是 `internal_error` 且退出码是 1」。控制器侧的同一条约束由 `tests/acceptance/test_m0_ai_interface_smoke.py:66-69` 的 `EXTRA_CODES` 逃生口钉住。

审查给的两个收敛选项是「补一份 ADR 批准它」或「把它提升进 `ErrorCode`」。本 ADR 选前者，理由如下。

## Decision

**`internal_error` 保持封闭集之外，作为一个控制器侧（以及启动器侧）的 catch-all 码存在，并由本 ADR 作为它的决策记录。** 具体约束六条：

1. **它是什么。** 它是「harness 自己坏了」的标记：控制器（或它前面的启动器）遇到了一个没有任何处理器认领的异常，因而不可能给出一个有意义的分类。它**不描述**访客、宿主、包或机器的任何状态——那正是 `ErrorCode` 其余成员的职责。
2. **它为什么故意在封闭集之外，而不是集里。** 四条各自独立的理由：
   - **语义不同类。** PRD §10 的封闭集枚举的是**被测系统的故障**：包预检、安装链、宿主/QEMU/QMP、镜像与产物、控制器/验证器/预算。`internal_error` 不指其中任何一类；把它放进同一个枚举，会让它看起来像一个可诊断、可补救的条件，诱使调用方像分支一个机器故障那样分支它。
   - **它的健康值是零，而封闭集里没有「恒不该发生」的成员。** 其它每一个码都是某条命令在某个已知条件下**故意**抛出的；`internal_error` 是控制器自身的推理失败。一个行为正确的构建发射它零次。把它列进封闭集，会让「零次发射」看起来像一个未使用的枚举成员，而不是设计目标。
   - **封闭集是对外产品契约，这个码不是。** PRD §9.1「scripts branch on failures without parsing text」的前提是每个码都携带稳定含义。「我们不知道发生了什么」不是项目能对消费者承诺的稳定分类。
   - **它必须能被 PowerShell 镜像而不导入 Python。** `unipad.errors` 是 Python 枚举，而调用链 7.2 已确认启动器是**手工**镜像 `_CODE_TO_EXIT` 的。一个只活在「一份 ADR + 每侧一个字面量」里的码，比一个活在被 PowerShell 逐字转写的枚举成员里的码 更难漂移。
3. **它必须永不掩盖已知故障（G1，当前已交付）。** `main` 的处理器顺序是承重墙：`except _HelpRequested`（`:2761`）→ `except UniPadError`（`:2764`）→ `except KeyboardInterrupt`（`:2767`）→ `except Exception`（`:2769`）。已知条件抛 `UniPadError`，因此永远先被认领。**把一条已知条件变成未预期异常，和发射 `internal_error` 是同一个缺陷类**——本轮工作树里有两个已被测试钉住的先例：调用链发现 11（`_coerce_install_result` 引用了一个不在作用域内的 `command`，`NameError` 逃进 catch-all）与启动记录的 `int(record["pid"])`（`ValueError` 逃进 catch-all）。两者都已修好，且都有「未修复时测试先红」的记录。这正是 G1 由测试而非期望维持的证据。
4. **它必须永不破坏「恰好一个 JSON 对象」（G2，当前已交付）。** `cli.py:2769` 的 `# noqa: BLE001 - never leak a traceback instead of JSON` 是这条约束的现场形式：catch-all 的存在使任何异常都不可能以回溯到达 stdout。`tests/acceptance/test_m0_ai_interface_smoke.py` 对十二条命令的 `json.loads` 整缓冲断言、`tests/unit/test_controller_cli.py` 的 `"NameError" not in json.dumps(result.document)` 从两头钉住它。
5. **退出码必须从码推导，不能与码并排手写（G5，当前已交付）。** 启动器侧 `Get-ExitCodeForErrorCode` 的形状（调用方问函数，而不是在旁边写一个退出码）正是为此存在；`test_launcher_script.py` 的断言同时钉住码与退出码，使两者不可能再各自漂移。控制器侧由 `UniPadError.exit_code` 的 `default` 提供同一个性质。
6. **提升是有条件的，不是被禁止的。** 若将来 PRD §10 的封闭集被修订出一个「harness / internal」类，或出现**第二个**封闭集之外的码，本 ADR 即被取代：要么把两者一起提升进 `ErrorCode`（并同步 `_CODE_TO_EXIT` 与启动器镜像），要么把第二个禁掉。逃生口从「一个例外」变成「一种模式」的那一天，就是重新决定的那一天。

## 它必须永不破坏的保证（逐条对现状）

| # | 保证 | 现状 | 证据 / 缺口 |
| --- | --- | --- | --- |
| G1 | 永不掩盖已知故障 | **已交付** | `cli.py:2764` 先于 `:2769`；调用链 11 与 pid 两个先例已修且测试钉住 |
| G2 | 永不把回溯放到 stdout | **已交付** | `cli.py:2769` 的 noqa 注释 + 冒烟测试的整缓冲 `json.loads` |
| G3 | 每一次发射都可观测 | **部分交付** | 信封携带 `code` / `message` / `details.exception_type` / `command` / `status` / `exit_code`，且启动器侧同码同退出码有独立字面量钉住。**但 `main` 不写实验日志、不写账本**（`:2744-2777` 无任何 store 交互），所以一次崩溃只留在 stdout，不进 PRD 9.2 的证据集。这与调用链发现 5（`install.json` 与账本追加之间的窗口）和 1.5（半写状态）是同一个形状 |
| G4 | PRD §9.2：无账号、无令牌、无用户文档 | **未交付 —— 已实测两处** | catch-all 的 `str(exc)`（`cli.py:2774`）与 budget 的四处路径（见下）；用户名走查测试当前正对 budget 那处判红 |
| G5 | 退出码从码推导 | **已交付** | 见 Decision 5 |
| G6 | 零发射是目标，不是覆盖缺口 | **未钉住** | 没有任何测试断言「沙箱里十二条命令都不发射 `internal_error`」 |

### G4：catch-all 会把宿主绝对路径原样放到 stdout 上（实测，修复已移交）

`cli.py:2774` 的消息是 `f"unexpected controller error: {type(exc).__name__}: {exc}"`，`str(exc)` **原样**进入 `error.message`。在一个异常的消息引用了路径的宿主上，这就是 PRD §9.2 的泄露：本机的绝对路径内嵌 Windows 账户名。

实测（scratch 探针，把 `cli._launcher` 临时换成一个抛 `FileNotFoundError` 的函数，跑完即恢复，未改动仓库任何文件）：

```
FileNotFoundError with a host path -> exit=1 code='internal_error'
    message: "unexpected controller error: FileNotFoundError: [Errno 2] No such file or directory: '<宿主用户目录>/.../secret.ipa'"
    details: {"exception_type": "FileNotFoundError"}
```

三点使这不是理论风险：

- catch-all 的**全部意义**就是处理未预期的事，所以「消息里可能带路径」是按构造成立的，不是巧合。
- 现有的用户名走查（`test_m0_ai_interface_smoke.py:275-296`）**够不到它**：那条测试只在沙箱里驱十二条命令，而沙箱里没有任何命令抛未预期异常。
- 同一个缺陷类在本项目已经发生过多次，其中一处**此刻仍然在世并且正被现有测试判红**，见下。

### G4 附：同一个缺陷类还有一处在世，且现有测试正对它判红

用户名走查测试今天**仍然 xfail**（`--runxfail` 下报 `AssertionError: doctor named the Windows account on stdout`），但它失败的原因**已经不是** marker 里写的那个。marker（`test_m0_ai_interface_smoke.py:261-274`）把原因记成 `cli.py:816` 的 `sys.executable`；那一处已在 Round A 修好（现在是 `safe_location(sys.executable)`，`cli.py:824`）。当前真正失败的是另一处，实测定位：

```
data.budget.usage.usage_file = <宿主用户目录>/.../budget-usage.json
```

源头是 `src/unipad/controller/budget.py`：

| 位置 | 内容 |
| --- | --- |
| `budget.py:339` | `"usage_file": self._usage_path.as_posix()` —— `--budget-usage` 原样进 doctor 信封 |
| `budget.py:197`（经 `:340` 的 `config_source`） | `self._config_path.as_posix()` —— `--budget-config` 原样进同一个信封 |
| `budget.py:188` / `:195` | `path=self._config_path.as_posix()` 进 `config_invalid` 的 details |

四处都是「调用方给的路径原样 `as_posix()`」。收敛形状与 G4 主体相同（`unipad.host.paths.safe_location`，或只保留末段 / 相对于一个已声明根的形式）。**因此 `xfail(strict=True)` 的 marker 现在既不能删（测试仍在失败），它的 reason 文本也已过期（指向一个已修好的位置）**——这正是 strict 绊线该有的信号：缺陷没死，只是换了地方。

**以上全部不由本 ADR 的文件范围修复。** 收敛形状（供属主参考，不代为决定）：catch-all 的 `message` 只保留异常类型与一个稳定短语，`details` 增加一个经 `unipad.host.paths.safe_location` 处理过的位置字段；或者让 `UniPadError.to_dict`（`errors.py:98-99`）对 `message` 统一施加同一层 redaction，使下一个实例不可能在别处出现。属主与确切位置见文末「移交」。

## Consequences

**正面**

- `internal_error` 从一个「代码自称需要一份不存在的记录」的状态，变成一个「有记录、有保证、有取代条件」的状态。规范 CG-4 关闭。
- 「为什么不在封闭集里」被写成理由而不是留白，后续任何人想提升它都必须先反驳这四条，而不是凭口味。
- G1/G2/G5 三条已交付的保证被显式列出并附上证据，重构时不会被无意破坏。

**代价 / 负面**

- 封闭集仍然不是字面意义上的封闭：`test_m0_ai_interface_smoke.py:66-69` 需要一个 `EXTRA_CODES` 逃生口。这是本决定的直接代价，记录在案而不是掩盖。
- 调用方要分支这个码仍然得硬编码字符串；它不在 `ErrorCode` 里，IDE 补全与 `_CODE_TO_EXIT` 都看不到它。
- G4 未交付：本 ADR 批准的这个码当前会把宿主账户名放到 stdout 上。这是本 ADR 必须同时记下的一笔未结账。

**未解决 / 现场项**

- G3 的「只进 stdout、不进证据集」与 G4 的 redaction 都改的是 `src/unipad/controller/cli.py`、`src/unipad/controller/budget.py` 与 `src/unipad/errors.py`，均不属本 ADR 文件范围，移交如下。
- 真机（真实 iPadOS 客体）上 catch-all 的表现 UNVERIFIED：本机从未到达过能让未预期异常自然产生的深度。

## 四字段

| 字段 | 状态 |
| --- | --- |
| 实现状态 | **CODE COMPLETE（代码阶段）**。`internal_error` 作为控制器侧 catch-all 的代码形状在 `cli.py:87-90` 与 `:2767-2776` 早已存在；本 ADR 补齐它自称必需的决策记录、保证清单与取代条件。未改动任何源码 |
| Windows/模拟测试状态 | **PASS（既有套件，本 ADR 未新增测试）**。`tests/acceptance/test_m0_ai_interface_smoke.py`（`EXTRA_CODES` 逃生口 + 十二条命令整缓冲 `json.loads` + 错误码全封闭断言）、`tests/acceptance/test_launcher_script.py:427-435`（启动器同码同退出码）、`tests/unit/test_controller_cli.py:785-805` 与 `:935-956`（两个「不得逃进 catch-all」的先例）实测通过。**唯一的 `x` 是 `test_no_envelope_carries_the_windows_username`**，它 xfail 的原因是 `budget.py:339` 而非 marker 所写的 `cli.py:816`——这正是 G4 未交付的现场证据，不是套件退化 |
| 现场验证状态 | UNVERIFIED —— 无真实客体；catch-all 在真实安装链上的可达性与消息内容需现场各跑一次 |
| 阻断原因 | G4 的 redaction 需要改 `src/unipad/controller/cli.py` / `src/unipad/controller/budget.py` / `src/unipad/errors.py`，按属主规则不在本 ADR 文件范围内；G3 的证据落盘同理 |

## 什么会改变这个决定

1. **PRD §10 的封闭集被修订出一个 harness / internal 类。** 那就把它提升进 `ErrorCode`，同步 `_CODE_TO_EXIT` 与启动器镜像，并由一份取代本 ADR 的新 ADR 记录。
2. **出现第二个封闭集之外的码。** 逃生口不再是例外而是模式：要么两个一起提升，要么把第二个禁掉。不得静默允许第二个。
3. **`EXTRA_CODES` 长出第二个条目。** 这与第 2 条是同一件事的测试侧信号，应同案处理。
4. **有消费者需要为 `internal_error` 增加「重试」之类语义。** 那意味着它开始携带信息，而携带信息的码属于封闭集。
5. **G1 被破坏**：任何已知条件开始以未预期异常的形式到达 catch-all。那是缺陷，不是提升的理由；修法始终是让那个条件抛 `UniPadError`（本轮两个先例的形状）。
6. **启动器镜像与控制器常量在字符串或退出码上不一致。** 那一天本 ADR 的「跨两侧同一标记」前提即失效，需要先恢复一致再谈其他。

---

# 第二部分（附录）：调用链发现 13 的评估 —— 18 个助手名跨 5 个 PowerShell 脚本

> **本节是评估，不是决定。** 调用链 7.4 要求的是「整合者赖以给重构划范围的事实」，不是「批准一次重构」。本 ADR 不为重构背书；重构若要立项，需要它自己的 ADR（本项目的先例：ADR-0004 把一条路线拆成四步并给每步设证据门槛）。

## 重复本身不是缺陷 —— 它是一份写下来的决定

`scripts/windows/Invoke-HostInventory.ps1:65-70`：

> Helpers. They are **intentionally duplicated** in every entry script: each .ps1 must run and be auditable on its own, with no dot-sourced module that could go missing when a single entry point is copied to a host.

四个入口脚本各带一条同样的横幅（`Get-Diagnostics.ps1:39`、`Start-UniPad.ps1:78`、`Test-ArtifactDownload.ps1:64`、`UniPad-Launcher.ps1:119`：「Helpers (duplicated per entry script; see Invoke-HostInventory.ps1)」）。这个理由是真实的：一个被单独拷到宿主机上的 `.ps1` 不能依赖一个可能没被一起拷走的模块。**因此「改成 dot-source 一个公共模块」直接违反这份写下来的决定**，重构必须在保留「每个脚本自足可拷走」的前提下消除分歧——这也是下面所有结论的约束。

实测规模（对 5 个脚本做花括号匹配提取全部函数体后统计）：

- 18 个助手名出现在 ≥2 个脚本里；
- 这 18 个名字的全部拷贝合计 **60,249 字符**；
- 扣除每个名字的一份规范拷贝后，**重复字符 41,269（约 40.3 KB）**；
- 5 个脚本的函数体合计 69,202 字符（67.6 KB）。

调用链审查在工作树之前的旧树上记的是「~46.8 KB」；Round A 改过两个脚本（`Start-UniPad.ps1` 补了 `Get-ControllerVerdict` 的一行，`UniPad-Launcher.ps1` 加了 `internal_error` 分支与 latest-run 规则），故数字有位移。上表是当前工作树的实测值，方法已写明，可复算。

## 枚举：18 个名字、各在哪几个脚本、逐份字节是否相同

行号是**当前工作树**的实测值（审查记录的行号已因 Round A 的编辑位移）。`sha` 为函数体（`function` 行到匹配的右花括号）的 SHA-256 前 10 位；`chars` 为字符数。脚本缩写：`GD`=Get-Diagnostics.ps1、`IHI`=Invoke-HostInventory.ps1、`SU`=Start-UniPad.ps1、`TAD`=Test-ArtifactDownload.ps1、`UL`=UniPad-Launcher.ps1。

| 助手名 | 脚本数 | 文本 | 各拷贝（行范围 / 字符数 / 体哈希） |
| --- | --- | --- | --- |
| `ConvertTo-JsonString` | 5 | 逐字节相同 | GD 42-60 (744c, 54ed001b17); IHI 107-125 (744c, 同); SU 117-135 (744c, 同); TAD 103-121 (744c, 同); UL 195-213 (744c, 同) |
| `Write-StructuredError` | 5 | 逐字节相同 | GD 62-81 (793c, b33828dbdd); IHI 127-146 (同); SU 137-156 (同); TAD 123-142 (同); UL 215-234 (同) |
| `Stop-UniPadScript` | 5 | 逐字节相同 | GD 83-96 (442c, 5e62d9addd); IHI 148-161 (同); SU 158-171 (同); TAD 144-157 (同); UL 236-249 (同) |
| `Test-CommandAvailable` | 4 | 逐字节相同 | IHI 83-90 (267c, 405c20ac50); SU 93-100 (同); TAD 79-86 (同); UL 158-165 (同) |
| `ConvertTo-Text` | 4 | 逐字节相同 | IHI 92-105 (458c, 26c2f96a32); SU 102-115 (同); TAD 88-101 (同); UL 180-193 (同) |
| `Get-Tail` | 3 | 逐字节相同 | IHI 468-479 (366c, 9f7d129274); SU 449-460 (同); TAD 462-473 (同) |
| `ConvertTo-NativeArgumentString` | 5 | **分歧（文本）** | GD 98-141 (1607c, e31ddc0252); IHI 179-222 (1607c, 同); SU 189-232 (1607c, 同); TAD 175-218 (1607c, 同); **UL 277-310 (1286c, 6ffccf2e46)** |
| `Invoke-NativeCapture` | 5 | **分歧（行为）** | **GD 143-191 (2029c, 3412e7b0af)**; IHI 224-268 (1990c, e3c48b14de); SU 234-278 (1990c, 同); TAD 220-264 (1990c, 同); **UL 312-349 (1671c, 02c350e4bd)** |
| `Get-ConfiguredPython` | 4 | **分歧（行为）** | IHI 71-81 (284c, 28ae86b586); SU 81-91 (284c, 同); TAD 67-77 (284c, 同); **UL 138-156 (752c, 5cb6895cf0)** |
| `Get-ExitCodeForErrorCode` | 4 | **分歧（文本）** | IHI 163-177 (570c, c088f4ef1d); SU 173-187 (570c, 同); TAD 159-173 (570c, 同); **UL 251-275 (1244c, c68408b78e)** |
| `Get-ControllerFallbackSnippet` | 4 | **分歧（文本）** | **IHI 270-281 (593c, 4f680f1bc5)**; SU 280-286 (316c, 462844c972); TAD 266-272 (316c, 同); UL 351-357 (316c, 同) |
| `Invoke-UniPadController` | 4 | **分歧（行为）** | **IHI 323-383 (2905c, e019c186e1)**; SU 288-334 (1901c, 4dd1198b81); TAD 274-320 (1901c, 同); **UL 359-399 (1799c, e4ba8eea8c)** |
| `Test-PayloadError` | 4 | **分歧（文本）** | IHI 385-400 (606c, 3d4c23a1c0); SU 372-387 (606c, 同); TAD 365-380 (606c, 同); **UL 430-442 (446c, 7df0baf1c9)** |
| `Get-Sha256Hex` | 2 | **分歧（文本）** | **IHI 430-466 (1419c, 9442e57876)**; **TAD 425-460 (1360c, ff1a12a9cd)** |
| `Get-JsonPayload` | 3 | **分歧（文本）** | SU 336-370 (1193c, 63fb97ac3b); TAD 332-363 (1028c, 571533f168); UL 401-428 (980c, cf1516b380) |
| `Get-ControllerVerdict` | 2 | **分歧（文本）；行为已一致** | SU 389-433 (2043c, d1084e12b3); UL 444-476 (1408c, e2a940f4b7) |
| `Test-UsageRejection` | 2 | **分歧（文本）** | IHI 283-302 (920c, c35a2f26be); TAD 382-400 (864c, cb8e5a1b0d) |
| `Write-Note` | 2 | **分歧（行为）** | **SU 435-447 (289c, 318da4943f)**; **UL 478-491 (599c, f7f997e9a9)** |

**对审查数字的一处修正。** 调用链 7.4 把 `Get-ExitCodeForErrorCode` 列为「byte-identical」，并数出 11 个分歧名。当前工作树上它**是分歧的**（UL 多一个显式 `'internal_error'` 分支和一段 `.DESCRIPTION`），所以分歧名是 **12** 个；但那是在 Round A 给 UL 加上 `internal_error` 分支之后才分歧的，审查看的是更早的树。更重要的是：**12 个文本分歧里只有 4 个是行为分歧**（下表 B 组），其余 8 个经差分实测行为完全一致——审查的「11 diverged」把「文本不同」和「行为不同」混成了一个数，而这决定了重构的工作量（B 组 4 个要判「哪份对」，A 组 8 个只需统一文本）。

## A 组：文本分歧但行为一致（实测 0 差异）

以下每一个都在 PowerShell 里把两份拷贝同时加载、喂同一组输入、比较输出，**差异数全部为 0**：

| 名字 | 差分实测 | 差异只在哪 |
| --- | --- | --- |
| `Get-ExitCodeForErrorCode` | 16/16 个码（含 `internal_error`、`image_not_found`、`artifact_missing`、`qemu_launch_failed`、`verification_failed`、`evidence_incomplete`、`restore_failed`、`image_hash_mismatch`、`bogus_code`、空串）全部同退出码 | UL 多一个显式 `'internal_error' { return $script:ExitFailure }`，其行为与它替换掉的 `default` 分支逐字节相同；另有一段 `.DESCRIPTION` 说明「码与退出码不得并排手写」 |
| `ConvertTo-NativeArgumentString` | 11/11 组参数（含空格、尾随反斜杠、成对尾随反斜杠、内嵌引号、`C:\...\file.txt`、`--json {"k": "v"}`）输出相同；唯一的空串实参被两份以同一个 `ParameterBindingValidationException` 拒绝 | 4 份带完整的 `.DESCRIPTION`（解释 `CommandLineToArgvW` 规则），UL 那份只剩 `.SYNOPSIS`——**丢掉的正是「为什么这样引号」的理由** |
| `Get-ControllerFallbackSnippet` | 返回串完全相同 | IHI 多一段 `.DESCRIPTION`（说明它镜像 `pyproject.toml` 的控制台入口、argv 经 `UNIPAD_ARGV` 以 JSON 数组传递所以不涉 shell 引号） |
| `Get-JsonPayload` | 9/9 段文本（纯 JSON、前后缀噪声、非 JSON、空、纯空白、数组、裸字符串、两个相连对象、前导换行）解析结果相同 | 三段 `.DESCRIPTION`（「容忍前导进度文本，靠首尾花括号重试」）的有无与多行/单行排版 |
| `Get-Sha256Hex` | 同一 7 字节文件摘要相同；对不存在的路径两份都返回 `''` | IHI 的 `.DESCRIPTION` 多最后一句「so the digest is computed the same way everywhere」 |
| `Test-PayloadError` | 13/13 个 payload（含 `error` 为对象、为空白串、键缺席、空对象、无已知键）判定相同 | IHI/SU/TAD 有完整 `.DESCRIPTION`（「控制器信封总带 `error` 成员，成功时为 null，所以「存在」不等于「失败」」），UL 只有 `.SYNOPSIS` |
| `Test-UsageRejection` | 5/5 段 stderr 文本判定相同；非 2 退出码两份都返回 `False` | `.DESCRIPTION` 举的例子不同（`doctor --json` vs `fetch --verify-only`），代码逐字节相同 |
| `Get-ControllerVerdict` | 13/13 个 payload（见下）裁决相同 | SU 多一段解释 `$names` 那一行为何存在的注释；UL 把若干单行 if 压成一行 |

**`Get-ControllerVerdict` 的行为一致是本轮刚刚争得的，并且已用变异证明。** 调用链 7.3 的活缺陷（SU 缺 `$names = @($Payload.PSObject.Properties.Name)` 一行，PowerShell 无块作用域，两个 `-contains` 循环恒 False，任何 payload 都返回 `indeterminate`）已在 Round A 的工作树里修好。差分方法：两份 `Get-ControllerVerdict` 与**各自脚本自己的** `Test-PayloadError` 同时加载（共 4 个函数，第二份的判决函数与其错误谓词都改名，否则测试会只测到一份谓词——这一点下面 C 组单独说）。

- 基线：**0/13** 分歧。
- 变异（在 scratch 拷贝里删掉 SU 那份的 `$names` 一行，即 7.3 的原缺陷）：**8/13** 分歧，SU 对每一个本该 success/failure 的 payload 都返回 `indeterminate`，与审查记录的现象一字不差。

## B 组：行为分歧（4 个）——分歧在哪、哪份对

### B1 `Get-ConfiguredPython` —— 探测的那份是对的

- IHI `:71-81` / SU `:81-91` / TAD `:67-77`：读 `$env:UNIPAD_PYTHON`，否则**无条件返回 `'python'`**。
- UL `:138-156`：`UNIPAD_PYTHON` 优先，否则依次试 `python`、`py`，每个都要过 `Test-CommandAvailable` **且** `Test-UniPadInterpreter`（真的 `import unipad`）；没有一个解析成功就返回 `''`，让调用方报 `blocked` 而不是猜。

**UL 那份是对的。** 差分实测（把两个谓词换成桩：`python` 与 `py` 都在 PATH 上，但只有 `py` 能 `import unipad`）：

```
Start-UniPad.ps1 copy      -> 'python'     # 错的解释器
UniPad-Launcher.ps1 copy   -> 'py'         # 对的解释器
```

若两个都不能导入，UL 返回 `''` → `UniPad-Launcher.ps1:756-757` / `:769-770` 报 `blocked: no interpreter found (tried UNIPAD_PYTHON, python, py)`；非探测那份返回 `'python'`，只被 `Test-CommandAvailable`（SU `:493`）挡住「不在 PATH」这一种，挡不住「在 PATH 但不能导入 unipad」——后者会一路走到控制器报 `No module named`，连直接模块回退也失败，操作者看到的是「控制器失败」而不是「解释器选错」。

**已付出的代价（调用链 7.4 已记，当前工作树仍然如此）**：`UniPad-Launcher.ps1:610` 用探测版算出 `$python` 并在 `-WhatIf` 计划里报告它（`:654`），但 `start` 动作在 `:620-623` 是**委派**给 `Start-UniPad.ps1` 的，后者在 `:492` 用非探测版**重算**一遍。启动器探到的结果在 start 路径上被丢弃。

### B2 `Invoke-NativeCapture` —— 三份身体，没有一份是普遍对的

| 拷贝 | `Process::Start` 失败时 |
| --- | --- |
| GD `:143-191` | `try/catch` 包住，**返回** `ExitCode = -1` + `StdErr = 异常消息` |
| IHI / SU / TAD | 不包，**抛**（落进各脚本的外层 catch） |
| UL `:312-349` | 不包，**抛**；返回对象压成一行 |

差分实测（`FilePath` 指向一个不存在的二进制）：

```
Get-Diagnostics copy : threw=            result=ExitCode=-1 StdErr='<本地化的 .NET 消息>'
Start-UniPad copy    : threw=MethodInvocationException  result=none
```

**哪份对取决于脚本，重构不能简单地「选一份」。** GD 的 docstring 自己写了理由：它是诊断扫掠（"which would abort a diagnostics sweep half-way through"），一次探针失败不该中断整轮；启动器那一侧有外层 catch 负责把任何异常变成一个结构化错误对象，抛出来信息更多。可执行的收敛形状是**二选一并让四个脚本都接受**：要么统一返回对象、让调用方把 `ExitCode -lt 0` 当失败（信息严格更多，外层 catch 仍然留给真正意外的故障），要么统一抛、给 GD 的扫掠循环加自己的 try/catch。**顺带记一笔 PRD 9.2**：GD 那份把 `$_.Exception.Message` 原样放进 `StdErr`，本机是中文 ANSI 码页的本地化串，且 .NET 消息会引用出错的值——这与启动器外层 catch 已经处理过的是同一缺陷类（`test_launcher_script.py:440-444` 只报告异常**类型**）。

### B3 `Invoke-UniPadController` —— 超集那份是对的，且已证明向后兼容

- IHI `:323-383`：**三次尝试**。主调用 → 「No module named」时走直接模块回退 → 调用方给了 `ReducedArguments` 且 `Test-UsageRejection` 为真时用缩减向量重试。docstring 明写 "Two and only two conditions are retried, never anything else"，并说明真实失败原样返回。
- SU `:288-334` / TAD `:274-320`：**两次尝试**（无 `ReducedArguments` 参数）。
- UL `:359-399`：两次尝试，返回对象压成一行。

差分实测（`UNIPAD_PYTHON` 指向一个打印 `unrecognized arguments: --verify-only` 并 `exit 2` 的假控制器）：

```
two-attempt copy  : ExitCode=2 UsedFallback=False Invocation='... -m unipad.controller fetch --verify-only'
superset copy     : ExitCode=2 UsedFallback=True  Invocation='... -m unipad.controller fetch'
superset, no reduced arg: ExitCode=2 UsedFallback=False
```

**IHI 那份是对的（超集），而且采纳它不会改变另外三个脚本今天的行为**——第三行就是证明：不给 `ReducedArguments` 时，`$usageRejected` 恒为 `$false`，行为与两次尝试版逐字节相同。它还用了 `New-ControllerResult`（IHI `:304-321`，只存在于 IHI）与 `ConvertTo-Text`，对已经是字符串的 `StdOut` 是恒等变换，同样不改变行为。

### B4 `Write-Note` —— 两份都对，统一必须保留开关

- SU `:435-447`：`$Json` 为真 → stderr；否则 → stdout。
- UL `:478-491`：**恒 stderr**。

差分实测（分离 stdout/stderr）：

```
--- normal mode ($Json = $false) ---   STDOUT: start-copy-note
--- json mode ($Json = $true) ---      STDOUT: (空)
STDERR: launcher-copy-note / start-copy-note / launcher-copy-note
```

**两份在各自的脚本里都对，所以「挑一份」会破坏另一个。** SU 在 `:546-548` 的注释写得很清楚：「The controller payload is always relayed verbatim on stdout; in -Json mode it is the only thing on stdout (notes go to stderr) so it can be piped straight into a JSON parser.」生产路径上 `UniPad-Launcher.ps1:623` **总是**加 `-Json`，所以 notes 走 stderr；UL 没有 `-Json` 开关且它的 stdout 永远是逐字节的控制器信封或串口日志，所以必须恒 stderr。可执行的收敛形状：统一成带开关的那份，并让 UL 一侧的开关默认/恒真。

## C 组：给重构属主的可执行结论

1. **不要 dot-source。** `Invoke-HostInventory.ps1:65-70` 的「每个 .ps1 自足可拷走」是一份写下来的理由，不是懒惰。收敛形状只能是「一份规范源 + 一个把它同步进 5 个脚本并可复算的步骤」，或「一份规范源 + 一个断言 5 份逐字节相同的测试」。
2. **工作量按 A/B 两组分开排。** A 组 8 个名字只需要统一文本（其中 6 个的差异**全部**在 docstring 里——被丢掉的恰好是「为什么」），零行为风险；B 组 4 个需要先判「哪份对」，其中 B1/B3 已有确定答案（探测版 / 超集版），B2/B4 需要先定策略再动手。
3. **B1 的扩散要先修。** `UniPad-Launcher.ps1:610` 探到的解释器在 `:620-623` 委派给 `Start-UniPad.ps1:492` 时被丢弃。这不只是重复：它是「启动器报告用一个解释器、实际用另一个」。
4. **回归测试的形状必须逐脚本隔离谓词。** `tests/acceptance/test_start_script_verdict.py` 已经是对的范本：它从真实脚本里抽取函数（花括号匹配，不 dot-source，因为这些脚本会启动访客），把第二份**连同它调用的谓词一起改名**，喂同一 payload 矩阵，断言两份「既一致又都对」。本次评估中一个反例说明了为什么必须这样：第一版差分只改了名 `Get-ControllerVerdict` 而让两份共用一份 `Test-PayloadError`，于是删掉 `Test-PayloadError` 里的 `$names` 时**判决差分是 0/13**（两份都调到了被改的那份谓词），只有谓词差分暴露了它。共用的谓词是一个隐藏耦合，测试必须把它也按脚本隔开。
5. **一个测试断言把 A 组钉成字节相同。** 项目已有先例：`test_start_script_verdict.py` 就是「从真实脚本抽取 + 差分」的形状，A 组可以同样逐字节断言；B 组则断言「行为一致 + 各脚本的策略被显式选择」。
6. **`Get-ControllerVerdict` 的文本分歧现在只剩注释与排版。** 修它的收益已经拿到（调用链 7.3 关闭）；剩下的是把 SU 那段解释 `$names` 为何存在的注释搬进 UL 那份，使「为什么」不在两个脚本里只存在一份。

## 本轮工作树中已经关闭的两项（与 13 同源，供整合者勾掉）

- **调用链 7.3 / 发现 2（`Get-ControllerVerdict` 永不报告成功）**：已修，`$names` 一行在 `Start-UniPad.ps1:409`；`tests/acceptance/test_start_script_verdict.py`（14 测试）钉住两份一致且都对。上面的变异证明复现了原缺陷（8/13 分歧）并证明当前树对它是免疫的。
- **调用链 7.5 / 发现 12（启动器的 `logs` 把「最新 run」实现成「最新目录」，可能选中 `runs/lifecycle`）**：已修。`UniPad-Launcher.ps1:554-576` 现在镜像控制器自己的规则——只认带 `experiment.json` 的目录、按日志 mtime 排序、按名字做确定性 tie-break，与 `cli.py:732-751` 的 `_latest_run`（取 `(mtime, id)` 的最大值）在排序方向与 tie-break 上一致。残余风险只有一处，量级很低：两侧读 mtime 的精度不同（Python `st_mtime` 浮点 vs .NET `LastWriteTime`），亚秒级差异可能让一侧看到平局而另一侧不看到；id/name 的 tie-break 使两侧在各自的可见输入上仍然确定。

## 移交（不属本 ADR 文件范围，按属主规则上报）

| 位置 | 需要的改动 | 属主 |
| --- | --- | --- |
| `src/unipad/controller/cli.py:2774` | catch-all 的 `message` 只保留异常类型与稳定短语；`str(exc)` 不得原样进入 `error.message`（G4 实测泄露） | `src/unipad/controller/cli.py` 属主 |
| `src/unipad/controller/budget.py:339`、`:197`（经 `:340`）、`:188`、`:195` | `usage_file` / `config_source` / `config_invalid` 的 `path` 不得把调用方给的绝对路径原样 `as_posix()`；用户名走查测试当前正对 `:339` 判红 | `src/unipad/controller/budget.py` 属主 |
| `tests/acceptance/test_m0_ai_interface_smoke.py:261-274` | `xfail(strict=True)` 的 reason 仍写 `cli.py:816`（已修好）；当前失败点是 `budget.py:339`。**不得删除 marker**（测试仍在失败），但 reason 必须改写，否则下一个修的人会去改一个已经对的地方 | 测试属主 |
| `src/unipad/errors.py:98-99` | 或在 `UniPadError.to_dict` 统一施加 redaction，使下一个实例不可能在别处出现 | `src/unipad/errors.py` 属主 |
| `src/unipad/controller/cli.py:2744-2777` | catch-all 发射时不写任何日志/账本（G3） | `src/unipad/controller/cli.py` 属主 |
| `tests/acceptance/test_m0_ai_interface_smoke.py:66-69` | `EXTRA_CODES` 逃生口与本 ADR 互为镜像；若本 ADR 被取代，此处同步 | 测试属主 |
| `scripts/windows/*.ps1`（B 组 4 个 + A 组 8 个） | 按 C 组结论重构；属主未定，需先立项 ADR | 启动器属主 / 待指派 |
| `tests/acceptance/test_start_script_verdict.py` | 扩展为覆盖 18 个名字的差分/字节一致性回归 | 测试属主 |
