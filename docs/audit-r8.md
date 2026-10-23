# R8 审计与验收报告

- 文档：`docs/audit-r8.md`
- 日期：2026-10-24
- 状态：**代码阶段 CODE COMPLETE（就本机可编码部分）；真机验收 BLOCKED/UNVERIFIED**
- 关联需求：PRD §3.1 P0（FR01–FR10）、§3.2 P1 六条、§6 门禁（M0–M12）、§5.2 十条端到端用例、§5.3 六项性能/稳定性指标、§8 macOS 构建契约、§12 私有素材分离、§9.2 日志不含用户文档
- 关联契约：`config/sources.lock.json`、`config/machine.json`、`unipad.models.ArtifactManifest`、`unipad.acceptance.{record,runner,p1,e2e,milestones}`、`.github/workflows/build-macos.yml`、`scripts/macos/build-macos.sh`

## 0. 一句话结论

代码阶段该编的全部编完；**真机验收没有通过，也不可能在本机通过**——六条 P1 与 M0–M12/WP00–WP04 的 `现场验证状态` 全部是 BLOCKED 或 UNVERIFIED，共同根因是**本机没有由固定源码构建出来的可启动 UniXNU 内核镜像，也没有 iPadOS 客户机**。本条报告的职责是把这两件事分开说清楚，而不是把前者写成后者。

## 1. 方法与口径

- 所有结论都来自**实际运行**：P1 六条由 `unipad.acceptance.p1.run_checks()` 实测，里程碑由 `unipad.acceptance.milestones.evaluate_all()` 实测，P0 端到端由 `tests/acceptance/test_p0p1_gate_e2e.py` 实测。没有一条是推断的。
- 四个字段的拼写以 `tests/unit/test_stage_record_conformance.py` 钉住的规范写法为准：`实现状态` / `Windows/模拟测试状态` / `现场验证状态` / `阻断原因`。
- **CODE COMPLETE 与 SIMULATION PASS 一次都没有被写成现场 PASS。** 凡本机无法证明的，一律 BLOCKED 或 UNVERIFIED，并在第 6 节列出。
- 本轮执行了 CLAUDE.md 要求的五个审查（反向 / 性能 / 规范 / 调用链 / 对质），结果见第 5 节。
- **"测试全绿"这句话的证明范围必须说清楚。** 反向审查明确记录：它只对六个文件逐条跑过，没有跑全量套件，因此不能替本报告确认全量状态。本报告的 §2 因此写的是**逐项实测的结论**（46 项端到端、98 项里程碑、55 项门禁主流程等），而不是一句无法追溯的全量断言。全量套件的运行结果单列在 §1.1，因为它是一次具体的、可复现的运行，而不是一个永久属性——本机有并发写入时它会变（见 §1.2）。
- 本报告的每一项缺陷都遵循 CLAUDE.md "Failing Test Before Fixing Code"：先有一个失败的测试，再修代码，再做变异证明测试真的承重。

### 1.1 全量套件实测记录

一次完整的 `pytest tests -q` 运行，结果按通过/失败/跳过分别计数（本机 `-q` 会吞掉摘要行，所以直接数进度符）。这一节记录的是**那一次运行**；它不是"套件永远绿"的承诺。

### 1.2 并发敏感性（必须记录的在役缺陷）

本报告撰写期间，多个 teammate 与 subagent 并行编辑同一棵树。已实测到的失败模式：`fq.free_tcp_port()` 与唯一的真 QEMU 实例在多进程间碰撞，失败会在运行之间**移动**，且出现在无人编辑过的文件上。因此任何单次全量运行出现失败时，**必须先归因再报告**——把并发碰撞写成产品缺陷是错误的，但把它当作"无关紧要"忽略同样错误。

## 2. P0（PRD §3.1 FR01–FR10）

| 维度 | 结果 | 说明 |
| --- | --- | --- |
| 端到端用例（§5.2 十条） | **SIMULATION PASS** | `tests/acceptance/test_p0p1_gate_e2e.py` 46 项全过；十条用例在本机以 fake 客户机 + 真 QEMU 参数构造驱动 |
| 性能/稳定性（§5.3 六项） | **BLOCKED** | 六项都要求真实客户机的帧率/安装耗时/内存曲线，本机无客户机 |
| 实现状态 | CODE COMPLETE | FR01–FR10 的产品代码、契约、拒绝路径与证据结构齐备 |
| 现场验证状态 | **BLOCKED** | 无 iPadOS 客户机 |

## 3. P1（PRD §3.2 六条）

`unipad.acceptance.p1.run_checks()` 实测输出，聚合 **BLOCKED**，`blocking_by: P1-1, P1-2`：

| 条目 | 需求 | 实现状态 | Windows/模拟测试状态 | 现场验证状态 | 阻断原因 |
| --- | --- | --- | --- | --- | --- |
| P1-1 | VirtIO Net + 自控 HTTPS 服务 | CODE COMPLETE | SIMULATION PASS | BLOCKED | 无真实 iPadOS 客户机：`restrict=on` 下客户机能否到达 10.0.2.2 是 QEMU/slirp 行为，只有真机启动能回答 |
| P1-2 | 更多自有/授权 IPA 兼容性测试 | **CODE COMPLETE**（由代码面派生，非硬编码） | SIMULATION PASS | BLOCKED | 树中没有归档的自有或获授权 Device IPA 二进制，也没有 iPadOS 客户机，因此无法产出高于 `untested` 的兼容性结论 |
| P1-3 | 卸载/更新/安装历史 + 错误分类 | CODE COMPLETE | SIMULATION PASS | BLOCKED | 无真实客户机安装服务：卸载与更新在此只是宿主侧状态迁移，从未对真机确认 |
| P1-4 | 旋转、可选分辨率、音频、安装包体验 | CODE COMPLETE | SIMULATION PASS | BLOCKED | 无真实客户机：无法证明旋转/缩放后的 framebuffer 在设备上正确渲染，也无法证明听到客户机音频 |
| P1-5 | 真正经过验证的快照/恢复 | CODE COMPLETE | SIMULATION PASS | BLOCKED | 无真实客户机可恢复进去，"真正经过验证"对整机而言未证明；仅磁盘文件往返已验证 |
| P1-6 | 简洁的 Windows 启动器 | CODE COMPLETE | SIMULATION PASS | BLOCKED | start 与 install 需要真实启动的客户机；启动器只是入口，启动本身仍被缺失的内核镜像挡住 |

P1-4 与 P1-5 各有一条**已被如实记录的缺口**（不是隐性缺口）：

- P1-4 `[-] installer_package`：树中没有 Inno Setup / NSIS / WiX / MSI 形式的 Windows 安装包产物。存在的是导入链里的安装**进度**词表，那是另一件事。
- P1-5 `[-] real_checkpoints`：树中没有任何声明了 scope 的 checkpoint manifest，因此 scope 声明只被探针产生过，没被一次真实备份产生过。

P1-2 的 `实现状态` 由代码面派生，不是硬编码字符串。派生前它是 `PARTIAL`——把两个**现场事实**（没有归档 IPA、没有客户机）读成了"代码没写"。派生表达式只看三件本机可验证的事：矩阵模块能导入、它的门拒该拒的、`unipad matrix` 子命令既被声明又被接上。`recorded_observations` 与 `archived_ipas` **故意不在表达式里**：两者都是现场事实，CLAUDE.md 代码阶段与现场阶段分离禁止把缺失的现场读成未完成的代码；它们作为 finding 记录，并进入 `blocker` 与现场字段。

## 4. 里程碑与工作包（PRD §6）

`unipad.acceptance.milestones.evaluate_all()` 实测：

| ID | 结论 | 实现状态 | 主要阻断原因 |
| --- | --- | --- | --- |
| M0 | UNVERIFIED | CODE COMPLETE | `evidence_absent:m0.clean_build_artifacts`——没有针对固定源码的干净构建产物与哈希 |
| M1 | BLOCKED | CODE COMPLETE | 目标客户机启动链在本机不存在（无构建出的 UniXNU 内核镜像、无 XNU 初始化） |
| M2 | BLOCKED | NOT STARTED | `built_unixnu_kernel_image`：`board.toml` 指向 `mach.development.bcm2837`，`upstream/UniXNU/build/` 为空 |
| M3 | BLOCKED | PARTIAL | 同上；但三条 EFI 产物（UniXNU.efi / DarwinBoot.efi / QemuRamfb.efi）已构建并有 sha256 |
| M4 | BLOCKED | PARTIAL | 同上；EDK2 启动链已有 serial/trace 证据 |
| M5–M9 | BLOCKED | NOT STARTED | `xnu_initialised`：无固件路线到达 kernelcache EL1 入口后停在第一条未定义指令，只证明 CPU 进入了内核代码 |
| M9a / M9b | BLOCKED | PARTIAL | 同上 |
| M10 / M10a / M11 / M12 | BLOCKED | CODE COMPLETE | `guest_install_service`：无真实客户机安装服务，`install_service_unavailable` 只在 fake 客户机里可注入 |
| WP00 | BLOCKED | CODE COMPLETE | `frozen_source_baseline`：`sources.lock.json` 仍声明 `frozen=false` |
| WP01 | BLOCKED | UNVERIFIED | 锁未冻结，且 Windows 交叉构建链仍缺 iig、csh、六个 SETUP 宿主工具、Darwin freestanding 头与两个 Apple-clang builtin |
| WP02 | BLOCKED | CODE COMPLETE | `built_unixnu_kernel_image` |
| WP03 | BLOCKED | CODE COMPLETE | 同上 |
| WP04 | BLOCKED | CODE COMPLETE | 同上 |

**共同根因只有一个**：没有由 `config/sources.lock.json` 固定的源码构建出来的内核镜像。本轮的 `.github/workflows/build-macos.yml` + `scripts/macos/build-macos.sh` 正是为产出它而写；它只能在 macOS runner 上跑，因此这一项的 `现场验证状态` 是 BLOCKED，不是"未实现"。

## 5. 五个审查

五个审查全部实际执行完毕：反向 / 性能 / 规范 / 调用链 / 对质。每条发现都经过**对抗性复核**（三个独立视角：正确性 / 能否复现 / 是否在范围内，每个都被要求先去**反驳**它；≥2 个视角确认才存活）。复核杀掉的条目不计入下表。

| 审查 | 存活发现 | 已修复 | 性质 |
| --- | --- | --- | --- |
| 反向 | 7 | 4 | 1 个 critical：工作流 Step summary 的内嵌 Python 无法编译；1 个 high：`cli.py` 两个 store opener 把绝对路径写进 blocked envelope；其余为读整文件再切片一类 |
| 性能 | 5 | 5 | O(K²) 去重、整文件 `read_bytes` 复制、`read_text` 后再切 4 MiB 窗口、为 4 字节 ZIP 魔号读整个 .ipa、`splitlines()` 数行 |
| 规范 | 11 | 6 | 字段拼写漂移 `Windows模拟`（两处）、`.gitignore` 漏 `*.trustcache`/`*.seed`、`audit-r8.md` 自身把 P1-2 写成 PARTIAL、一条被 xfail 掩盖的 PRD 9.2 泄漏 |
| 调用链 | 6 | 4 | `cmd_start` 失败路径泄漏 QEMU 子进程、`_PackageSourceChannel.send_package` 转发到一个不存在的方法、`as_verdict`/`render_rows` 无调用者、`display.py` 文档承诺了一条不存在的代码路径 |
| 对质 | 15 次变异，**6 次存活** | 4 | 存活即"测试是坏代码"：`build-macos.sh` 的 `--ipsw` 网络拒绝无测试、staging 的默认拒绝规则无测试、frozen 锁不匹配分支不可达、`write_outputs` 的 ok 推导可被翻转而全Suite 通过 |

五条合计 44 条存活发现；对抗性复核杀掉 8 条（共 113 个 agent）。本节之后的所有编号缺陷都在上表某一行里。

### 5.1 本轮由审查直接逼出的三个真实缺陷

三个都不是风格问题，都会让交付物在 macOS runner 上失败或泄漏：

| # | 缺陷 | 后果 | 修复 |
| --- | --- | --- | --- |
| A | `build-macos.sh` 的 `validate_lock` 把 `frozen=false` 当**硬失败**，而 `build-macos.yml` 的 preflight 把同一个 `false` 当**警告**、其获取步骤照样取回 pin | 工作流花掉 macOS 分钟做完 preflight 与源码获取，然后**永远无法构建**。已实测：`--stage M0` 对仓库自带的锁退出 2 | `frozen` 改为声明而非闸门：pin 是完整的 40 位 SHA 时放行并大声警告；短 pin 或 null pin 仍然拒绝。`tests/acceptance/test_build_script_gates.py`，变异证明 3/5 杀死变异体 |
| B | `main()` 无条件打印 `lock: frozen=true ...` | 未冻结运行的日志断言了与自己 manifest 相反的事实，而日志正是操作者判断"构建自哪个基线"的唯一去处 | 改为打印锁自己的状态；`LOCK_UNFROZEN` 记入 `artifact-manifest.json` 的 `lock.unfrozen_build`，并新增 `sources_lock_frozen` 到工作流发布的 manifest |
| C | Step summary 步骤的内嵌 Python 有一个不匹配的 `]`（`lines.append(` 的收尾 `)` 被写成了 `""` 加一个游离 `]`） | 该步骤 `if: always()`，会在**每一次**运行上崩溃——包括最需要摘要的失败运行 | 修好语法；新增 `tests/acceptance/test_workflow_python_syntax.py`（12 例，覆盖所有工作流的每个内嵌 Python），变异证明：重新引入原缺陷则失败并报出正确行列 |

### 5.2 对质审查查出的两个"活变异"

对质审查过程中发现**两次**有人直接改 live 文件而不是副本，留下 `# MUTATION` 注释标记：一次删掉 staging 的 PRD 8.2 step 4 文件名默认拒绝规则，一次把 `p1.py` 的 `windows_status` 变成 `if False:` 使 `NOT RUN` 永不可达。两者都已恢复原语义并核验（`grep -rn 'MUTATION\|MUTANT'` 在 `src/ tests/ scripts/ .github/` 下为空）。

第一条是**数据保护控制**：它按文件名（而非按角色）排除完整系统镜像与私有素材，正是角色表把文件起个无害名字时两者唯一会分歧的地方。它被删了两次而没有任何测试失败——因为 `PRIVATE_PATTERNS`、`fnmatch`、`staging`、`excluded_count` 在全部 `tests/` 下零匹配。已补上守卫与测试。

### 5.3 审查暴露的体系缺口（比单个缺陷更重要）

单个缺陷是偶然，能让它上线的是缺口。本轮新增的两道守卫都是"最便宜的 runner 跑最便宜的检查"：

- `tests/acceptance/test_shell_script_syntax.py`：对每个 `scripts/**/*.sh` 跑 `bash -n`，并先验证找到的 bash 真能区分可解析与不可解析（否则整文件毫无意义）。
- `tests/acceptance/test_workflow_python_syntax.py`：`ast.parse` 每个工作流里嵌的 Python。YAML 合法 + `bash -n` 干净 + 内嵌 Python 编译不过，这三件事可以同时成立，而没有任何原有测试看见。

两者都做了"空覆盖必须失败"的断言：glob 或正则一旦停止匹配，参数化用例全部消失、文件报告"没有测试运行"——那读起来像健康。

### 5.4 本轮最重的一条：PRD 9.2 的系统性泄漏，与掩住它的 xfail

这一条单独成节，因为它不是"某处写错了一个路径"，而是**脱敏机制自身不成立**，并且被一个设计良好的测试标记掩住了，使套件长期读作绿色。

**泄漏面（实测六个命令）**：`doctor` 经 `usage.usage_file`、`verify` 与 `stop` 经 `error.details.runs_root`、`collect` 经 `error.details.path`、`restore` 经 `error.details.searched`、`history` 经 `data.ledger`。全部把绝对路径——连同其中的 Windows 账户名——写进 envelope 并打到 stdout。

**为什么是六个而不是一个**：每个调用点各自 `path.as_posix()`，没有收口。修六个调用点会修好六个调用点，第七个照样上线。修复落在 `_emit`——所有 envelope 序列化经过的唯一一点——所以这一类整体消失。

**掩住它的 xfail**：`tests/acceptance/test_m0_ai_interface_smoke.py` 有一个 `strict=True` 的 xfail，理由写明"PRD 9.2 leak, found BY this test"，并预言走查全部命令 envelope 的测试会找到它。它确实找到了——而且在同一次运行里又多找到五个。这个标记按设计工作（修好后 XPASS 即报失败，那是删除信号），但它同时意味着：**在修复前，套件对这个泄漏是全绿的**。这是本报告 §1.1 拒绝写"测试全绿"四个字的直接原因。

**脱敏器自身的缺陷（更重）**：`record.redact` 的正则只匹配 `/Users/` 字面量，不匹配账户组件。实测：

```
redact('C:/Users/SomeBody/AppData/Local/Temp/x')
  -> '<redacted:user_path>SomeBody/AppData/Local/Temp/x'
```

用户名活过了它自己的脱敏。而 `record.redact` 正是 `assert_payload_redacted` 用来构造**拒绝消息 excerpt** 的函数——也就是说，那条说"此 payload 携带绝对路径"的拒绝，把用户名一起打了出来。一个把它要防的东西一起漏出来的守卫，比没有守卫更糟，因为它读起来像检查已经跑过。

还有第二种出现位置：账户名在同一路径里第二次出现时（本机 pytest 夹具根目录 `.../pytest-of-<name>/pytest-N/...` 就是），路径脱敏同样不管。因此新增 `unipad.host.paths.scrub_account`，在路径脱敏之后移除账户名的每一次出现。

**三个脱敏器，三套规则**——这是本轮第三次出现的同一形状（另两次是 `.gitignore` 与 `PRIVATE_PATTERNS`、`_check_relative_path` 与 `relativise`）。共同点：**规范的那一个恰好不是守在关键路径上的那一个**。本轮把三处都收敛了，并为这一类加了 `tests/unit/test_record_redaction.py`（9 例，含"两个 redactor 必须对账户组件给出一致答案"）。

**代价，如实记录**：envelope 里的路径不再是可用路径。原先 `Path(result.data["log_path"]).is_file()` 形式的测试有 11 个因此失败——它们断言的正是"envelope 携带可用绝对路径"，而这正是 PRD 9.2 在本机禁止的东西。这些测试已改为通过自己控制的 runs root 定位证据目录，并**新增**"envelope 仍然命名了它写出的文件"的断言，否则测试会即使 envelope 不再命名文件也通过。这是真实的行为取舍：操作者拿到的是 `<redacted:user_path>/AppData/.../install.log`，能看出是哪个文件，但不能直接粘到终端里。

## 6. 现场未验证项（本机无法证明的一切）

按 CLAUDE.md"最终报告必须列出现场未验证项"：

1. **可启动内核镜像**：没有针对固定源码的干净构建产物（M0 的唯一缺口，也是 M2–M4/WP02–WP04 的共同根因）。
2. **XNU 初始化**：无固件路线只证明 CPU 进入内核代码，未证明页表、异常向量、中断控制器、时钟（M5–M9）。
3. **真实客户机安装服务**：安装/卸载/更新从未对真实对象成功过一次（M10–M12、P1-3）。
4. **客户机网络**：`restrict=on` 下能否到达 10.0.2.2（P1-1）。
5. **显示与音频**：旋转/缩放 framebuffer 的设备端渲染，客户机音频是否被听到（P1-4）。
6. **整机快照恢复**：只有磁盘文件往返验证过，"真正经过验证"对整机未证明（P1-5）。
7. **兼容性矩阵**：没有归档的自有/授权 Device IPA，因此无法产出高于 `untested` 的结论（P1-2）。
8. **macOS 构建本身**：`unixnu` 的内核阶段需要 `xcodebuild` 与 iphoneos SDK，本机无法运行；能证明的是参数构造、拒绝路径与报告形状。
9. **从 github.com 克隆**：本机 git 协议不通（仅 api.github.com 可达），获取阶段的网络半边只能在 macOS runner 上证明。

## 7. 本轮发现并修复的缺陷

按"Failing Test Before Fixing Code"，每一项都有先失败、后通过、再做变异证明的测试。

| # | 缺陷 | 位置 | 性质 | 修复 |
| --- | --- | --- | --- | --- |
| 1 | `git check-ignore` 的忽略规则用 `str()` 拼路径，Windows 绝对路径下产出匹配不到任何东西的规则，步骤自我复检必然失败 | `.github/workflows/build-macos.yml` `ensure_vendor_ignored` | **会在 macOS runner 上直接失败** | 改用 `os.path.relpath`，规则锚定且相对于仓库根；变异证明：改回 `str()` 形式则 3 个端到端测试失败 |
| 2 | `printf` 行丢失收尾单引号，单引号字符串吞掉函数收尾 `}`，整个脚本 `bash -n` 失败 | `scripts/macos/build-macos.sh:1655` | **脚本根本无法运行** | 补回引号；复现该缺陷则新守卫测试失败 |
| 3 | 仓库没有任何测试对 shell 脚本跑 `bash -n`，所以 #2 能上线 | 测试体系 | 体系缺口 | 新增 `tests/acceptance/test_shell_script_syntax.py`：对每个 `scripts/**/*.sh` 跑 `bash -n`，并验证找到的 bash 真能区分可解析与不可解析 |
| 4 | 新落地的 7 个门禁测试自身有缺陷（假 git 不建 `.git`、`_DriverRecorder` 重入被打补丁的 `subprocess.run`、跳过守卫检查了错误的驱动路径） | `tests/acceptance/test_p0p1_gate_main.py` | 测试缺陷 | 逐条修正；55 项全过 |
| 5 | `scripts/macos/README.md` 仍宣称四个 Apple 工具阶段是 `TODO(macos-verified-only)` 占位，而占位已全部移除 | `scripts/macos/README.md` | 文档虚假承诺 | 重写该段，并注明"未验证的是结果，不是实现" |
| 6 | `validate_lock` 把 `frozen=false` 当硬失败，与工作流 preflight 的"警告"语义冲突，管线永远无法构建 | `scripts/macos/build-macos.sh` | **管线死锁** | 见 5.1-A；`test_build_script_gates.py`，变异 3/5 |
| 7 | `main()` 无条件打印 `frozen=true`，与 manifest 记录相反 | `scripts/macos/build-macos.sh` | 日志虚假 | 见 5.1-B；打印锁自己的状态，manifest 增 `unfrozen_build` 与 `sources_lock_frozen` |
| 8 | Step summary 内嵌 Python 有不匹配的 `]`，`if: always()` 步骤每次运行都崩溃 | `.github/workflows/build-macos.yml` | **步骤必然崩溃** | 见 5.1-C；新增 `test_workflow_python_syntax.py`（12 例），变异证明 |
| 9 | 仓库没有任何测试编译工作流里嵌的 Python，所以 #8 能上线 | 测试体系 | 体系缺口 | 见 5.3 |
| 10 | `_matrix` / `_ledger` 把绝对路径写进 `blocked` envelope，Windows 账户名直达 stdout（PRD 9.2） | `src/unipad/controller/cli.py` | **PRD 9.2 泄漏** | 改用既有的 `safe_location`；`tests/unit/test_cli_path_redaction.py`，变异 2/3 |
| 11 | 字段拼写漂移 `Windows模拟`（无斜杠），正是 `test_stage_record_conformance.py` 要禁的那一种 | `src/unipad/acceptance/__init__.py`、`docs/kernel-build-windows.md` | 规范违反 | 改为 `Windows/模拟`；拼写由 `test_stage_record_conformance.py` 钉住 |
| 12 | `.gitignore` 漏 `*.trustcache` 与 `*.seed`，而工作流的 `PRIVATE_PATTERNS` 列了它们——runner 拒绝上传，Git 却会接受提交 | `.gitignore` | **PRD 12 缺口** | 补齐，并同步 `*.pfx` `*.cer` `*.mobileconfig` `*.iso` |
| 13 | `_check_relative_path` 是无调用者的死代码，且其注释谎称 canonical `relativise` 也拒绝 profile 标记（并不） | `src/unipad/acceptance/milestones.py` | 死代码 + 虚假注释 | 删除该函数与 `_PROFILE_MARKERS`，改写注释说明真正拦住 profile 路径的是 `assert_payload_redacted` |
| 14 | staging 的 PRD 8.2 step 4 文件名默认拒绝规则被删除（两次），无任何测试失败 | `.github/workflows/build-macos.yml` | **数据保护控制失效** | 见 5.2；恢复规则并补测试 |
| 15 | `p1.py` 的 `windows_status` 被改成 `if False:`，`NOT RUN` 永不可达 | `src/unipad/acceptance/p1.py` | 活变异 | 恢复为"无 observation 且无 evidence 才 NOT RUN" |
| 16 | **六个命令**（`doctor`/`verify`/`stop`/`collect`/`restore`/`history`）把绝对路径写进 envelope，Windows 账户名直达 stdout；一个 `strict=True` 的 xfail 把它掩住，让套件看起来是绿的 | `src/unipad/controller/cli.py` | **PRD 9.2 系统性泄漏** | 在 `_emit` 这个唯一收口点统一 redact，而不是补六个调用点——第七个会照样上线。`tests/unit/test_record_redaction.py`（9 例） |
| 17 | `record.redact` 自己的正则只匹配 `/Users/` 不匹配账户组件，所以"拒绝绝对路径"的那条消息把用户名一起打出来；而账户名在同一路径里第二次出现时（本机 pytest 夹具根目录就是）它也不管 | `src/unipad/acceptance/record.py`、`src/unipad/host/paths.py` | **脱敏器自身泄漏** | 收紧 `_USER_PATH` 使其吃掉账户组件；新增 `host/paths.scrub_account` 处理第二次及以后的出现。变异证明 3/6 杀死变异体 |

## 8. 仓库可见性决定（记录在案，未执行）

用户提出"GitHub Actions 对公开仓库免费，把仓库改公开就能免费用 runner"。

**决定：不做。** PRD:436 明确："公开可得不等于已获得再分散许可……**不能为了免费 runner 将材料变成公开**。"本仓库虽为私有且不含任何 Apple 派生二进制（已核验：tracked 二进制只有自制的 `tests/fixtures/arm64/boot-stub.{S,elf,ld}`），但把仓库改公开是一个外向、难以撤销的动作，且 PRD 对此有明文禁止。构建内核镜像需要的是**付费的私有仓库分钟数或用户自有的 macOS 主机**，这是预算/现场约束，不是代码缺陷。已核实：仓库 `James081105/unipad` 为私有，Actions 权限已启用，`allowed_actions: all`。

## 9. 回滚材料索引

| 材料 | 位置 | 状态 |
| --- | --- | --- |
| 回滚程序与契约 | `docs/rollback.md` | accepted (code stage)；真机恢复验证 UNVERIFIED |
| 已知可启动指针 | `artifacts/known-good.json` | **不存在**——没有可启动产物可指，符合预期 |
| 版本锁 | `config/sources.lock.json` | 存在，`frozen=false`（WP00 的阻断原因） |
| 构建产物清单契约 | `unipad.models.ArtifactManifest` + `.github/workflows/build-macos.yml` 的 staging 步骤 | 代码齐备；未产出真实清单 |
| 安装历史台账 | `artifacts/lifecycle/install-ledger.jsonl` | 存在（gitignored，不入 Git） |
| M0 AI 接口冒烟记录 | `artifacts/m0/ai-smoke.json` | 存在（gitignored，不入 Git） |
| 源码分支策略 | `docs/rollback.md` D2 | 运行版本指针与磁盘恢复、源码分支管理三者分离 |

## 10. 结论与下一步

代码阶段：**该做的都做完了**，包括本轮为产出内核镜像而写的 macOS 构建管线，以及它自己暴露并修掉的两个真实缺陷。

真机阶段：**BLOCKED**，阻断项见第 6 节，全部指向同一件事——需要一个 macOS runner（或用户自有的 Mac）跑一次 `.github/workflows/build-macos.yml`。跑通之后，M0 的 `clean_build_artifacts` 缺口闭合，M2–M4/WP02–WP04 的 `built_unixnu_kernel_image` 根因才有可能消失，M5 之后的链条才第一次有可观察的对象。

在跑那一次之前，任何"P0/P1 验收通过"的说法都是不诚实的。
