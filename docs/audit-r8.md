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

### 7.1 第九轮（2026-10-24，Windows 启动链）

这一轮的起点不是"写新功能"，而是"把已经绿了很久的东西真的跑一次"。它因此暴露的缺陷有一个共同形状：**代码路径存在、格式正确、测试全绿，而它从来没有被执行过**。

| # | 缺陷 | 位置 | 性质 | 修复 |
| --- | --- | --- | --- | --- |
| 18 | `_qmp` 在请求循环里 `return`，于是它在 `qmp_capabilities` 的回复后就返回，**从来没有把调用方给它的命令发出去**。后果全部长得像一次成功的运行：`query-status` 没发（`qmp_evidence` 记的是一条没人问过的问题的回复）、`screendump` 没发（每次运行零张截图）、`quit` 没发（每次运行都落到 `process.terminate()`） | `src/unipad/host/unixnu_boot.py` | **静默失效**，且被全绿的套件掩住 | 改成 `break`，循环后返回；`_watch` 只发一次 `query-status`。承重测试 `test_the_qmp_round_trip_speaks_the_protocol` 先以 `xfail(strict=True)` 失败，修复后按约定移除 marker |
| 19 | `docs/boot-windows.md` §1.3 写"收到 greeting 与 `query-status` 回复"。那句话是**过头了**：那是同一条 capabilities 回复记了两次 | `docs/boot-windows.md` | 文档虚假证据 | 改写为"QMP over TCP 能建连、能握手"，并把截图链路与优雅退出单列为未实测项（§4） |
| 20 | `board.toml [payload.adt] chip_id = "207"` 被当十进制读，与 `make-qemu-adt.py:68` 的 `int(value,16)` 冲突，读回校验必然误报 | `src/unipad/host/adt.py` | 校验失效 | 按十六进制解析并注释；测试钉住 |
| 21 | `board["payload.adt"]` 永远取不到——TOML 里它是嵌在 `[payload]` 里的表，点号名不存在 | `src/unipad/host/adt.py` | 配置读取静默失败 | `_block()` 逐段走 |
| 22 | `subprocess.run(["python3", ...])` 在 Windows 上 `FileNotFoundError [WinError 2]`：`CreateProcess` 用**父进程**的 PATH 解析可执行文件，不看 `env` 里那个 | `src/unipad/host/adt.py` | Windows 专属失败 | `shutil.which(command[0], path=env["PATH"])` 解析成绝对路径 |
| 23 | `scripts/qemu-entry/make-dt.py` 在设备仓库里找不到——它在 UniXNU。直接跑工具必失败 | `src/unipad/host/adt.py` | 缺输入 | `prepare_tree()` 按 `unixnu/tree.py:_copy_scripts` 的顺序合并两棵 `scripts/` |
| 24 | `UniXNU.efi`（固件真正启动的 UEFI application）构建了但从未收集；承载它的 350 MB 整树 artifact 在公开暴露清理时被删除，**字节已不存在于任何地方**，只剩 manifest 里的一条记录 | `scripts/macos/build-macos.sh`、`.github/workflows/build-macos.yml` | **交付物缺失**，且不可恢复 | `step_build_bootloader` glob 出它（缺一个 `die`、多一个也 `die`）并读 PE subsystem 验证是 application 而非 driver；`boot-inputs` 束把它列为第一项 |
| 25 | `EFI/BOOT/BOOTAA64.EFI` 必须是 PE subsystem 10，而手里只有 subsystem 11 的 driver。固件报"The image is not an application."，BDS 报"failed to load Boot0001 … Not Found"——**指名设备不指名原因**，读起来像文件不存在，而 `ls FS0:\EFI\BOOT` 证明它在 | `src/unipad/host/unixnu_boot.py` | 诊断信息误导 | 启动前读 PE subsystem，指名原因拒绝；runner 侧同样检查，复用同一个 `pe_subsystem`，两边不可能对"固件会怎么对待这个文件"产生分歧 |
| 26 | `mapfile` 是 bash 4+，macOS 自带 `/bin/bash` 3.2——新加的一段在 runner 上必然失败，而本机 `bash -n` 查不出来 | `scripts/macos/build-macos.sh` | **runner 专属失败** | 改用脚本既有的 `for candidate in glob; do [[ -f ]] && arr+=(...)` 惯用法（`plan_steps` 那里有注释说明为什么） |
| 27 | 构建失败时，staging / 大上传 / boot-inputs 三个步骤用默认 `success()` 被**跳过**，于是 `build-bootloader` 已经产出并记录的 `UniXNU.efi` 既没暂存也没上传也没收集 | `.github/workflows/build-macos.yml` | **失败即丢证据** | 三个步骤改 `if: always()`；staging 自己的默认拒绝仍然拦住"只有日志没有产物"的构建（PRD 7.4），所以always 发布的是证据不是绿灯 |
| 28 | `board.toml [payload] panic_screen = true` 让 payload 阶段编译 `xnu-panic-screen.c`，而它 `#include "Font.h"`（`:294`）来自一个既不是 feed、也不在两个 pinned 仓库里的 Mu-Silicium 检出。实测死法是 clang 的 `fatal error: 'Font.h' file not found`——指名 include 不指名原因，和 #25 同一种形状 | `scripts/macos/build-macos.sh` | **第二个阻塞点**，且诊断不可行动 | 跑 driver 之前先查该头文件，ledger 条目写明路径、两个查找位置、以及缺失的代价（只有 panic screen；entry probe 是同一个 patch 的另一半，两种设置都要，`payload.py:128-129`） |
| 29 | `boot-inputs` 束的 manifest 只记**存在**的文件：丢了 `payload.img` 的束，和本来就没产出 `payload.img` 的构建，单看 manifest 无法区分 | `.github/workflows/build-macos.yml` | 证据不完整 | 缺席条目记为 `{"bytes": null, "sha256": null, "absent": true}`，汇总行打印缺席数 |
| 30 | `boot-inputs` 目录不重建，上一次运行的残留会被当成本次证据哈希进 manifest；`STAGE_DIR` / `BUNDLE_DIR` 是声明了却没人读的死配置 | `.github/workflows/build-macos.yml` | 证据污染 + 死配置 | 开头 `rmtree` 后重建；两个变量改为真正读取（与 staging 步骤同一惯用法） |
| 31 | 工作流注释声称"整树不再上传"，而它上一步的 `path: staged/` 仍然上传 368,499,519 字节（run 40091901777 实测）。读者会把那条注释当成对整个工作流的描述 | `.github/workflows/build-macos.yml` | **注释误导** | 注释改写为明确声明 `unipad-build-*` 仍是整树、为什么还没改（`verify-artifacts.yml` 与 `publish-artifacts.yml` 都解析 `unipad-build-*` 并按 manifest 重哈希，收窄它是对那两个工作流的契约变更，不是这里的一行改动） |
| 32 | 本机 `C:` 卷跑到 0 字节可用，pytest 的编号临时目录工厂开始报 `OSError: could not create numbered dir … after 10 tries`，套件表现为"跑 10 个测试后挂住" | 主机环境 | 环境缺陷伪装成测试挂死 | 清掉已结束测试的 `tmp*` 暂存（实测释放 5.1 GB）；套件改用仓库外的 `TEMP` 运行。**注意：第一版把 `TEMP` 指到了 `local-private/` 底下，反而引出 4 个新的假失败，见 7.3** |

### 7.3 第九轮后半段：我自己引入的回归，与三个环境性假失败

启动链跑通之后，macOS 管线验收门是**红的**。这一节记的是"绿的提交把门搞红了"这件事本身，
以及三个差点被当成代码缺陷的环境问题。

| # | 缺陷 | 位置 | 性质 | 修复 |
| --- | --- | --- | --- | --- |
| 33 | `step_build_bootloader` 里 `UniXNU.efi` 缺失用 `die`，而它前面一步已经跑完、内核/DarwinBoot/manifest 记录都在——`die` 会把 `build-report.json`、`artifact-manifest.json` 和全部 artifact 记录一起丢掉，偏偏是在最需要证据的那次失败上 | `scripts/macos/build-macos.sh` | **失败即丢证据**（与 #27 同一类，但发生在脚本内部） | 改为 `ledger_add 'failed'; return 1`，与同一函数里其他后置条件失败一致；计划同样通过 `stage_failed` 停止 |
| 34 | `_SCRATCH_DRIVER` 的 `package` 分支只写 `build-darwinboot-efi.py`，不写 `UniXNU.efi`。于是 #24 的检查在这个夹具上是**无条件的 `die`**，6 个测试在 `assert result.returncode == 0` 全挂，且都到不了自己的断言 | `tests/acceptance/test_p0p1_gate_main.py` | 夹具不再忠于真实 driver（真实 driver 确实产出该文件，93,696 字节） | 夹具写一个真的 PE32+ subsystem-10 镜像（`pe_efi_application()`）；验证过正反两面：文本 → `None`，subsystem 11 → 11 |
| 35 | 同一夹具把 `src/unipad` 桩成一个空 `__init__.py`，于是 `unipad.host` 导入失败，subsystem 检查死于 `ModuleNotFoundError`——**唯一那道把 driver 挡在 ESP 外的检查，在门里等于没测** | 同上 | 测试覆盖虚假 | 改为 `shutil.copytree` 真实 `src/unipad`（纯 Python、无第三方依赖）；这也顺带修正了另一个问题，见下 |
| 36 | 桩掉 `src/unipad` 还让 seed 派生失败，于是 `test_a_stage_that_cannot_run_says_so…` 一直在断言 **seed** 理由——而真实 checkout 上 seed 是**派得出来**的，那个测试是为了错误的理由通过 | 同上 | 测试通过但原因错误 | 断言改为 QEMU 理由（真实 checkout 上真正拦住这一步的东西），并新增 `test_a_derived_platform_seed_is_kept_by_digest_and_never_echoed` 钉住种子只以摘要出现；seed 拒绝路径在 `tests/unit/test_host_adt.py` 已钉住，docstring 里写明为什么不在这里造一个够不着的夹具 |
| 37 | 接受门互斥锁 `acceptance.lock` 用 `O_EXCL` 创建，被 kill 的进程会把锁**永久留下**，之后同一个 run_id 每次都是 BLOCKED，报 "another acceptance evaluation holds this run"——指名一个并不存在的持有者。本机实测：我停掉两个后台 pytest，两个锁留下，两个测试从此全红 | `src/unipad/acceptance/runner.py` | **永久楔死**，且诊断信息说谎 | 锁里记录的 PID 先做存活检查，死者之锁被移除后重取；PID 复用风险写在 docstring 里（接受理由：永久楔死更糟） |

**三个环境性假失败，差点被记成代码缺陷：**

1. **`TEMP` 指到 `local-private/` 底下。** 我为绕过 C: 卷满把 `TEMP` 设成 `F:/iosvirtual/local-private/tmp`，
   于是 4 个测试的夹具根落在 `local-private/` 里，而工作流步骤按 PRD 12 **拒绝** `local-private/` 下的路径
   ——报错是"private material is never fetched or built here"，看起来像我破坏了数据保护规则。
   改用 `F:/unipad-tmp`（仓库外、F: 盘、不在 `local-private/` 下）后全部消失。
   **教训：为绕过磁盘问题改环境变量，会用一类新的假失败替换掉原来那类。**
2. **并发 pytest 互相删暂存树。** pytest 的 `tmp_path_factory` 只保留最近三个根，多个进程并发时
   会删掉彼此的 scratch 树。本会话先后出现过 15、8、6、0 四个不同的失败数，全部来自同一份代码。
   两个 teammate 各自纠正了自己两次，最终在**单进程**下得到 6，再修完夹具得到 0。
   **任何失败数在并发下都不可信。**
3. **`bash -n` 查不出 bash 3.2 不兼容。** `mapfile` 是 bash 4+，macOS 自带 `/bin/bash` 3.2，
   而 Git Bash 5 下 `bash -n` 完全通过。同一个文件第 2484 行自己写着这条约束，
   新加的代码却违反了它——是 teammate 抓出来的，不是任何工具。

### 7.4 这一轮最有价值的一条

#33 与 #27 是同一条教训的两次重复：**"写下来"和"活下来"是两件事**。#27 是工作流层面
（失败时跳过上传步骤），#33 是脚本内部层面（失败时 `die` 掉整个输出）。两次都是"在失败路径上
把证据扔掉"，而失败路径恰恰是唯一需要证据的地方。

#35/#36 则是另一种：**测试可能因为错误的理由通过**。桩掉 `src/unipad` 让 seed 派生失败，
于是那个测试断言 seed 理由并通过了两年都没人发现——真实 checkout 上它早就该断言 QEMU。
夹具越忠于真实 driver，测试越可能暴露自己一直在测错东西。



#18 与 #27/#28 是同一条教训的三次重复：**"构建阶段名称、Actions 绿色、二进制格式正确"都不等于"它跑过"**。#18 期间测试套件是全绿的，而一个 QMP 命令都没有发出去。#27 期间 Actions 是绿的，而唯一的交付物被跳过。#28 期间 driver 退出码非零、clang 报错清晰，却没有任何东西告诉操作员缺的是哪个文件。

因此本轮的交付判断标准只有一条：**证据本身被读过**。`boot-inputs/manifest.json` 的存在是为了让一份启动日志能指名它的内核；`if: always()` 是为了让一次失败的构建仍然留下它产出的东西；`pe_subsystem` 与 font 预检是为了把"含义不明的固件/clang 错误"变成"指名原因的拒绝"。

## 8. 仓库可见性决定（记录在案，未执行）

> **2026-10-24 更正：本条已被用户授权推翻，事件经过与后果见 `docs/public-exposure-audit.md`。**
> 用户为取得免费 macOS runner 明确授权将仓库改为公开，授权同时规定任务完成后恢复私有并核查已公开产物。
> 实测：仓库已恢复 **PRIVATE**（0 fork / 0 star / 0 watch、无 release），公开期间上传的 8 个约 350 MB 的
> `unipad-build-*`、4 个 `boot-inputs-*` 与 7 个 `build-logs-*` artifact 已全部删除，仅剩 34 个
> `unipad-preflight-*`（合计 125,962 字节，内容为主机清单文本）。无法撤回的部分也如实记在该文档里：
> 公开期间被第三方克隆的历史提交不能召回，CDN 缓存不承诺清除。
> 本条目保留原文，因为它记录的推理（PRD §12 的禁止条款）没有被授权删除，只是被用户的明确授权
> 一次性覆盖；下次再遇到同一个选择时，要重读的是 `public-exposure-audit.md` 而不是这一条。

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

## 10. 根卷轮实测（2026-10-25，事实 37–43）

这一节记录的是**本机 Windows QEMU TCG 上的一次次真实运行**，不是对进度的转述。所有数字都能在 `local-private/boot7`–`boot11` 里复现。

### 11.1 已经实测到的

| 项 | 结果 | 证据 |
| --- | --- | --- |
| 真访客启动到 BSD 阶段 | **VERIFIED-ON-HOST** | `boot8`：QMP over TCP 读活访客，`allproc` 走出 `kernel_task` pid 0，`msgbufp` 反卷出 17,084 字节完整 XNU 23.6.0 日志（APFS 已加载、Skywalk 自测全过、`bsd_autoconf: calling kminit`） |
| 根卷挂载侧 | **CODE COMPLETE + 本机实测 argv 被接受、设备被创建** | `build_argv(root_image=…)` 发出只读 `virtio-blk-device`（MMIO）+ `-global virtio-mmio.force-legacy=false`；`query-block` 报 `device=osroot inserted=True`，`esp` 仍是独立设备 |
| OS 根卷容器 | **本机解出，字节级校验通过** | `udif.py` 解出 6,077,580,800 字节；GPT 头 CRC32 0xd9e4279c 与分区项 CRC32 0xfa7a560c 双双匹配；399 个免库块与一份独立实现逐字节相同（0 处不符） |
| 内核等的 UUID 就在解出的镜像里 | **VERIFIED** | 唯一 APFS 卷的卷超级块（block 15900，oid 1026，type 0xd）在 `+0xf0` 处正是 `04769d262ca04825b443e2e5f88300f2`，与内核 `Waiting on … IOMedia … UUID 04769D26-…` 一致 |

### 11.2 阻断是什么，精确地说

**不是镜像，是内核配置。** 实测：把 3.3 GB 裸 APFS 的 SystemOS 镜像作为第二个 virtio-blk 挂上，访客走到与不挂盘时完全相同的位置（17,113 字节日志、`bsd_autoconf` 在、仍在等同一个 UUID），整个内核日志里除 `Waiting on` 那一行本身外**零**块设备枚举。

原因是配置而不是运气：`qemu_defconfig`（本机四个内核构建全部用它）没有 `CONFIG_XNU_NATIVE_VIRTIO_BLOCK`，而 `qemu_root_defconfig` 才加它，并且同时要 owner root chain 的七个开关、两个内核步骤之外产出的归档、Data seed 与 trust cache。全盘四个 `kernel.macho` 逐个查过，`NativeVirtioBlock`/`VerifiedRootProbe`/`NativeRamBlock`/`NativeLegacyDisplay`/`owner-data` 五个串**全部为 0**（`Libkern` 为 5，证明检索有效）。

**传输层也差一点就错。** `NativeVirtioBlock::probe()`（`NativeVirtioBlock.cpp:148-154`）在 provider 的 `compatible` 不是恰好 12 字节 `"virtio,mmio"` 时返回 `nullptr`，所以 `virtio-blk-pci` 永远绑定不上。第一版代码用的正是 PCI，而 QEMU 毫不抱怨地接受了那条 argv——跑得起来、日志正常，只有去读那个永远不会绑定它的驱动的 `probe()` 才发现错。

### 11.3 本轮同时修掉的门禁缺陷（这与根卷无关，但是同一次测量逼出来的）

1. **`_has_sparse_index_symbol` 只读文件最后 8 MiB**，理由「Mach-O 字符串表在 `__LINKEDIT`」——对*已链接*镜像成立，对本机启动的 prelinked 内核不成立（符号在 17,058,661 / 51,859,456，窗口从 43,470,848 开始）。于是对一个**有**该符号的内核报「不存在」，七个里程碑因一个不成立的理由停在 BLOCKED。改成全文件分块扫描；stock kernelcache 全文 0 次命中，是阴性对照，仍须拒绝。
2. **`_probe_esp_efi_chain` 只认 `MiniEfi: alive`**（旧实验的*测试*载荷），于是在一个 DarwinBoot 早已真实运行的本机上记 `darwinboot: False`。
3. **对抗审查发现第 2 条的修法本身放宽了门禁**：匹配裸 `DARWINBOOT:` 让一句 `Error: DARWINBOOT: image not found` 或一条注释就能置真并压制 PRD 209 的 veto（实测 M3 从 FAIL 变 UNVERIFIED 且无拒绝）。已改成只匹配 `DARWINBOOT: hand-off to the kernel`。

### 11.4 现场未验证项（本节不得冒充的部分）

- **launchd PID 1 与后续守护进程：未获取。** 根卷未挂载，`vfs_mountroot` 一直阻塞，访客在 `bsd_autoconf` 之后没有新进程。观察通道本身已就绪（30 项合成测试 + 一次真访客实测），缺的是被观察的对象。
- **macOS 预处理链：代码完成，未在 runner 上执行。** `scripts/macos/build-macos.sh` 的 `build-os-root` stage 与 defconfig 切换是本轮新增；它需要 `hdiutil`、`diskutil`、brew `openssl@3` 与 Xcode，本机全无。`not_run` 理由由脚本自己记录，不由本报告代述。
- **OS 成员的全图解码：未在本机完成。** 3814 块里 3415 块是 LZFSE，Python 标准库没有解码器。GPT CRC 与卷 UUID 的结论是由一份独立实现得出的；`udif.py` 与之交叉验证的部分是 399 个免库块。
- **新根目录拓宽了 ONSITE 证据面。** 已实测一个放在 `local-private/boot7/run/guest.log` 的合成 guest log 能满足 M3 全部三个 `_ONSITE` 要求（同一文件放在带 simulation 标记的路径下则正确被判 SIMULATION 且什么都不满足）。这是既有类别的拓宽（`out/real-boot` 早就如此），当前视口内 guest log 数为 0。

### 11.5 第二轮：端到端复测、runner 上的真阻断、CI（事实 44–45，2026-10-25）

这一轮没有换架构，是拿第一轮的结论去跑、然后修跑出来的东西。分四件。

**(1) 端到端回归跑通了，而它先暴露探针自己的一处假声明。** `scripts/windows/probe-root-volume.py` 的 `BootInputs` 把 `devicetree`/`darwinboot`/`unixnu` 写成 `run/"esp"/...`——也就是 `stage_esp` 自己要写的目的地，而它在拷贝任何东西之前先 `shutil.rmtree(esp)`，所以这些输入永远交不到 `boot()` 手里，模块里「The same inputs `unixnu_boot.boot()` takes」这句注释在唯一要紧的地方是假的。回归因此以一个裸 `FileNotFoundError` 失败——一个错的输入在没有任何东西检查它时长什么样。改成指向 payload 构建自己的产物，设备树是唯一没有新鲜来源的输入（会写出它的构建因缺 `FrameBufferSerialPortLib/Font.h` 失败了），于是加了 `verify_adt_matches_board()`：拒绝一份不发布 `board.toml [payload.adt] root_match_volume_uuid` 的树，因为这个属性正是决定内核在等什么的那个。**boot15 实测**：ESP 恰好五个 `stage_esp` 文件，设备树按板级 UUID 校验通过，argv 带根盘，msgbuf 反卷出 **17,112 字节**（sha256 `0cb19122e0bb5557dac35e189ab6a14263e53588e4a30e18a00011d4f8487e4c`），标记 `['Waiting on', 'bsd_autoconf', 'IOMedia']`，`allproc` 只有 `kernel_task` pid 0 状态 `run`，exit 0。同一轮里三个我自己的缺陷各自由我自己的检查抓到，顺序都是反的：暂存文件数检查期望 5 项而 `stage_esp` 返回 5 个 payload 文件加一个 `_esp` 记账键（一次正确的暂存被拒绝）；`guestobserve --run-id` 是字面量 `boot9` 而目录是 `boot15`（样本声称了自己不是的那次运行）；CLI 测试断言 `"root volume attached" not in unattached` 而 `"no root volume attached"` 包含这个子串（断言在它本该排除的分支上通过）。

**(2) 阻断的真正位置在 macOS runner，不在本机内核——这是本轮最重要的发现。** 此前记成「本机内核没有 `CONFIG_XNU_NATIVE_VIRTIO_BLOCK`」，那是症状。链条：M2 的计划里 `build-os-root` 就排在 `build-kernel` 前面，而 `build-kernel` 只在 `root_chain_present` 为真时才换成 `qemu_root_defconfig`；可是 `build-os-root` 要一个**本地** IPSW，workflow 只调用一次 `build-macos.sh`、从不传 `--ipsw`，它自己的 IPSW 步骤取完 kernelcache 就把 6.8 GB 镜像删掉了（`src/unipad/host/ipsw.py:26`）。所以 runner 上 `build-os-root` 恒 `not_run`、无链、内核恒 `qemu_defconfig`。**空间约束让这件事不能靠抬高地板绕过**：`DEFAULT_MIN_FREE_GB=8` 而 macOS runner 只有约 14 GB 空闲，把 IPSW 留整个 job 会直接 `die 'blocked'`。`step_build_os_root` 第 (4) 步自己写明了预期的两遍顺序，所以要查的正是 workflow 有没有做第二遍。**这与事实 43 记的「缺 LZFSE 解码器」是两件不同的事**：后者决定本机能不能自己造出根镜像，前者决定 runner 能不能造出*能读*根镜像的内核——混为一谈正是前者被藏住的原因。

**(3) 内核清点数错了，已更正。** 本文 11.2 此前写「本机四个内核构建」。全盘 `local-private/**/kernel.macho` 共 24 个文件、按 sha256 去重后是 **5 个真构建**（四个 51,859,456 字节、一个 46,415,872 字节；其余 13–73 KB 是占位桩不是内核），五个全部零命中 `NativeVirtioBlock`/`virtio,mmio`/`CONFIG_XNU_NATIVE_VIRTIO_BLOCK`/`VirtioBlock`，对照针 `Libkern`=5、`IOMedia`=28 证明检索有效。少数一个不改变结论，但一个数错的计数会让人以为清点做过了。

**(4) CI 两个红灯都是缺依赖声明，不是代码缺陷。** `tests/integration` **从来没有在 CI 上跑过**：integration job 校验 pin（每次都真过）、证明 HEAD 等于锁里的 commit，然后在裸 runner 解释器上跑 `python -m pytest tests/integration`——没有 pytest，收集阶段就死。`tests/unit` 收集不起来：`test_workflow_contracts.py` 在模块级 import `yaml`，runner 上没有 PyYAML。两处都修了（`[dev]` 加 `pyyaml`；integration job 补上与 preflight 相同的安装步骤）。声明依赖而不是改成「import 失败就 skip」，是因为那个测试存在的全部意义就是让一类**在通常地方看不见**的失败变得可见—— reusable workflow 调用带 `timeout-minutes` 会在 GitHub 建 job 图时就失败，run 零秒完成、`jobs` 为空、没有日志可读——而一个在 CI 上跳过的检查恰恰在那里是不可见的。

### 11.6 本轮新增的未验证项（不得冒充的部分）

- **`qemu_root_defconfig` 内核：仍未构建，因此 launchd PID 1 仍未获取。** 阻断已定位到 runner 的 IPSW/两遍问题（11.5(2)），修法已派 teammate 独立评估。修好之前，任何「根卷可挂载」的说法都是不诚实的。
- **OS 成员全图解码：解码器已进仓库（`src/unipad/host/lzfse.py`，867 行，纯标准库），但 6 GB 全图未在本机端到端跑完。** 已实测：从 `044-76040-038.dmg` 枚举出 3814 个非终止块（396 raw / 3 零填充 / 3415 LZFSE，与既有记录一致），并用它对**真实块**解码——块 0 是 53 字节压到 512、块 512 是 108 压到 512、块 1024 是 209 压到 16,384，全部成功。这是移植正确的证据，不是全图解码完成的证据。
- **macOS runner 的 Font.h 修复：代码已改（第三个 pin `uefi_sheng`、`--uefi` 传递、字体 sha256 校验），未在 runner 上验证。** 锁里每一项都已独立核验：仓库 `UniXNU/uefi-sheng` 存在（HTTP 200、默认分支 `sheng`、BSD-2-Clause）、commit `20ac23d5e0937106c2129303dfc931b3390ae660` 是 `sheng` 的尖端、`Font.h` 在锁定路径下 3692 字节、sha256 `d2a3687db67b069f4e34a961e17fd28b0242986e3fa8dd8a7545cab70834fa8e`、git blob sha1 `2ea15a28fe1ad2ddddf1cbbac766aeceb9243183`。`patch-xnu-entry-probe.py:183`、`xnu-panic-screen.c:294`、`board.toml:94` 全部落在所说的行。**该轮曾有一处假声明已被抓出并更正**：它写「导出 `UNIXNU_UEFI` 对 payload 不生效」，而 `cli.py:136` 是 `ctx.uefi = ctx.option('uefi', 'UNIXNU_UEFI')`、`config.py:116` 回落到 `os.environ`——本机跑驱动自己的代码实测：导出后 `ctx.option` 返回该路径，未导出返回 `None`。`payload.py:142` 的覆盖是透传不是清空。选 `--uefi` 仍然对（进 argv.log、`need_uefi()` 读同一个 option），但理由必须是真的。
- **全量套件在 `tests/acceptance` + `tests/unit` 同进程下有一个跨套件污染失败**：`test_doctor_is_structurally_stable_across_invocations` 在全量下红、在 `tests/unit` 单独跑时全绿（2206 tests / 0 failures / 6 skipped）。`run_cli` 是进程内调用 `cli.main`，pytest 按字母序先跑 acceptance 后跑 unit，所以污染来自 acceptance。**哪个键在动尚未查明**（junit 的失败文本在关键处被截断），已派 teammate 定位；在查明之前不得通过「多 pop 一个键」让它变绿——那个测试的契约就是「doctor 里不是测量的部分不得移动」。
- **`.tmp-bt/`：38 MB teammate 暂存工作区曾让 `test_the_guard_covers_every_shell_script` 在一次内容完全正常的树上变红**（八份 `build-macos.sh` 副本）。已修：该守卫现在把「本仓库」定义成 git 跟踪的内容，暂存目录叫什么都不会再触发；并实测它仍然能在真正新增一个未被点名的脚本时失败并点名。暂存目录本身仍在本仓库内，已要求移出。

### 11.7 第三轮：根链在 runner 上第一次跑到 step 9，阻断是两个固定仓库之间的版本错位（事实 48–49，2026-10-25）

这一轮没有换架构，是把上一轮定位到的 runner 洞（11.5(2)：只跑一遍、不传 `--ipsw`）补上之后，看链到底能跑多远。

**(1) 链第一次真正跑起来，而且跑得很远。** 补了两遍构建 + 第三个 pin（`uefi_sheng` 的 `Font.h`）+ stage 提到 M4 之后，run 40187668943 里 `build-os-root.py` 第一次越过它此前每次死掉的第一个子步。通过的有：`build-storage-abi.py`（真 QEMU 观测一次）、`build-owner-systemos-policy.py`（日志原文 `PASS: original SystemOS signature and every 806400 block verified by real C/RSA`）、`build-verified-root-probe.py`、`NativeVirtioBlock` 与 `NativeRamBlock` 的 verified-root 绑定（各 190 个槽位）、APFS 容器从 32 MiB 长到 1 GiB 并 `fsck_apfs -n -x` 通过（卷 `OwnerData`，UUID `675761EF-5622-4F5B-89DF-4CAD5BA456F6`）、`packed_ramdisk.py`（1,073,741,824 字节 seed → 1,245,328 字节 packed）。第一遍的 `build-kernel` 也真的产出了 `qemu_defconfig` 内核：`kernel.macho` sha256 `412e38386dfb0ef22b22d1436084f001bb627af0e55c39d14beb348cf951d037`、51,859,456 字节；`DarwinBoot.efi` sha256 `f78ad56a6635a71026fee36dc40c0cd74ca60044012438d33ded65eb992055a0`、27,136 字节；`UniXNU.efi` sha256 `59b7aa748848328bc88d363d57180c9faba491eccfd5e7847565e033d4858016`、93,696 字节；`payload.img` sha256 `415594fb5b8393a70fb8ed193e5274ca4efc57c4060062b5fcc229596c5f689e`、201,326,592 字节。第一遍 `stages: ran=8 not_run=1 failed=0`。

**(2) 它死在链的 step 9，而根因不是代码缺陷也不是环境，改代码修不好。** 报错是 `drivers/NativeMobileDisplay/NativeLegacyDisplay.cpp:206:59: error: only virtual member functions can be marked 'override'` 与 `:207:13: error: no member named 'getRefreshAttributes' in 'IOMobileFramebufferLegacy'`。逐层查下去：被钉死的设备仓库 `xVanTuring/UniXNU-qemu`（唯一分支、已在尖端 `97b75b1f`）的 `build-native-legacy-display.py` 从一张**固定 slot 表**生成 `OriginalLegacyFramebufferABI.h`，表里没有 slot 301；而 UniXNU 的 gitlink 把 `drivers/NativeMobileDisplay` 钉在 `c031d882`（`feat(display): report the panel refresh rate`），该提交恰好加了 slot 301 的 override。**「把 slot 301 补进生成器」不是可行修法**：同一个脚本的 owned-set 不变量会再拒绝一次（`require(i in (0,1,7,14,84,86,87,141,155,184,188,288,290,296) or i in pure, 'Only qualified own methods and guarded leaves override')`），而设备仓库没有更新的提交可换（`git ls-remote` 只有 `main` 一个分支）。所以子模块回退一个提交到 `eb9c949`，其 override 集合正是生成器声明的那一个。代价只是 `sheng-vsync` 的刷新率上报，由一个本板不传的 boot arg 把守，不在任何 root mount / launchd 路径上。

**(3) 第一次修法放错了位置，是 runner 自己告诉我的。** 只在锁文件里改 sha、并只在链步骤之前强制树里的检出，run 40199915736 于是在 `prepare-sources` 就退出 1：`pinned sources are missing and this script performs no network access, so the upstream driver cannot be allowed to fetch them: submodule:drivers/NativeMobileDisplay`，后面每一步全 skip，构建一次都没被 exercise。原因是 `unixnu fetch` 用**驱动仓库自己的 gitlink 表**初始化模块子模块（`feeds.py:159-165`），而 `offline_missing()` 拿磁盘上的 HEAD 与**锁**比——锁才是权威（PRD 8.1）。所以分歧必须发生在** sources 落地的位置**，即 acquisition 步骤，在 `unixnu fetch` / `unixnu tree` 之后、cross-check 之前；链步骤那份强制保留为第二道幂等保障（它保证的是链真正编译的那份副本，即树里的检出）。

**(4) 两个测试的红色是宿主环境，而分辨它花了一次测量而不是一次猜测。** 全量套件 **3114 tests / 0 failures / 5 skipped**（在没有其他 pytest 进程、树静止的条件下取得；此前每一次全量数字都被并发争用或移动的树污染过）。此前那两个失败与我的改动无关（`_lock_payload()` 把 `submodules` 置空，锁里那一项到不了这两个测试），真实原因有两层且都量过：**WSL 的 Ubuntu 没有 `ext4.vhdx`**（`wsl -l -v` 两个发行版 Stopped，全盘找不到该文件），于是 PATH 上每个 `bash` 都是启动器，`C:\Windows\System32ash.exe` 以 `Bash/Service/CreateInstance/MountDisk/HCS/ERROR_FILE_NOT_FOUND` 退出，脚本一行都没读到，表现出来却是 `build-report.json` 的 `FileNotFoundError`——读起来像「构建脚本坏了」；退到能跑的 Cygwin bash 后撞第二层：pytest 的 `tmp_path` 在 `C:`（5.1 GiB 空闲）而 `DEFAULT_MIN_FREE_GB=8`，脚本以 exit 3 与「5 GiB free, 8 GiB required」拒绝。修法是量出来的：`_bash()` 现在**运行**每个候选而非只解析名字；`_out_dir()` 在默认卷放不下时把输出搬到有空间的卷且**不传 `--min-free-gb`**（8 GiB 门禁仍真实生效）；`_report()` 只在脚本自己报「not enough free space」时具名 skip，其余任何缺报告的情况升级成断言失败。**同一个红色，如果直接放宽门禁或删掉断言，两次都会变绿，而每一次都等于埋掉一条真实契约。**

**(5) 我自己写的第一版测试没有牙齿，已抓出。** 新增的排序断言用 `text.index("apply_submodule_divergences()")` 找调用点，而 `def apply_submodule_divergences():` 里就含这个子串，且定义位于 `run_driver("fetch")` 之上——断言在证明不了任何事的情况下通过。改成匹配调用形式；把调用移到 fetch 之前，它立刻失败（实测）。同轮还有一次：`text.index("cross_check(")` 找到的是定义，改成 `= cross_check(feeds`。

### 11.8 本轮新增的未验证项（不得冒充的部分）

- **`qemu_root_defconfig` 内核与 `os_root_status=pass`：仍未取得。** 显示驱动的 sha 分歧已按上述理由处置并推送（`cbdce24` / `f96e70d`），但验证它的 run（40200941051）在本节撰写时仍在跑，其 `os_root_status` 与第二遍是否构建 `qemu_root_defconfig` **未读**。链在显示之后还有 `build-original-system-trust.py` 一个从未跑过的步骤（它产出 payload 的 `--trust-cache`，缺它 AMFI 会以 `SECURITY_POLICY` 拒绝 launchd）。
- **本机 QEMU 上的根卷挂载与 launchd PID 1：仍为零。** 需要的三样东西（能读根的 `qemu_root_defconfig` 内核、OS 根卷镜像、SystemOS 与 trust cache）本机都不全；本机已有的内核全部零命中块存储驱动（事实 40）。
- **WSL 破损未修，也不该由我修。** `ext4.vhdx` 缺失意味着 PATH 上的 `bash` 全部不可用；两个 `build-macos.sh` 测试靠搬到有空间的卷跑起来了，但这是在 Cygwin bash 下成立的，WSL 恢复后需要复跑一次确认没有分叉。
- **`drivers/NativeMobileDisplay` 回退一个提交的代价未经实测。** 「只影响 `sheng-vsync` 刷新率上报」是从源码与调用点推出的，不是在访客里量到的；它不影响 root mount / launchd 路径这一结论同样只有静态证据。

### 11.9 第四轮：根链十步里九步第一次全过，第十步死于一个工作区相对路径（事实 50，2026-10-25）

这一轮只改了一处，而它是上一轮那个版本错位修好之后**露出来的下一个洞**。`build-os-root.py` 的十个子步里，此前每次都在 step 9 死；step 9 通过之后，第一次看见了 step 10。

**(1) step 9 真的过了，而且连带整条显示链都过了。** run 40203832707：`NativeLegacyDisplay: -Werror build and actual ARM64/iOS 17 object audit passed`，`NativeHidInput` / `NativeKeyStoreEvents` / `NativeGOPPresenter` 同样通过，`PASS: original Legacy has 358 slots with nine abstract leaves`，`Built genuine original Legacy inheritance/dispatch; seven backend leaves still guarded, runtime lifecycle unqualified`。连同此前已过的步骤，十步里九步成立：storage-abi（190 槽 / 176 绑定）、VerifiedRoot 主机校验（7 组真实 RSA）、OS 策略（**1,483,785 块**）、SystemOS 策略（**806,400 块**）、verified-root probe、两个 verified 块驱动、1 GiB OwnerData 与 packed ramdisk。

**(2) step 10 报的是 `FileNotFoundError`，而它指认的对象不是原因。** 报错指着 `<tree>/refer/iPad_Pro_HFR_...ipsw`，读起来是「refer 镜像不见了」。真相是：workflow 的 (2) 段用 `ls "$REFER_DIR"/*.ipsw` 取路径，`$REFER_DIR` 是工作区相对的；`build-os-root.py:166` 做 `ipsw.symlink_to(Path(args.ipsw).resolve())`，而这一步的 cwd 是**树**（`cd "$tree" && python3 scripts/build-os-root.py`），于是相对路径被解析到 `<tree>/ipsw-work/refer/...`。镜像一直在 `ipsw-work/refer/`，只是**两个目录不是同一个地方**。

**(3) 这个缺陷还盖住了另一件事。** 同一个相对路径也喂给 `extract-target-volume.py`。这一遍没在抽取两个卷时失败，只是因为卷已经在 `.cache` 里（上一遍留下的）；一个从空 `.cache` 开始的 run 会在更早的地方以同一个根因死。修成绝对路径同时盖住两者。

**(4) 修法与它的测试。** workflow 在把 `refer` 交给模块之前把它变成绝对路径（`cd "$(dirname ...)" && pwd`，不用 `realpath`——不是每个 runner 镜像都有），并加一条「不是文件就拒绝」的守卫。测试 `test_the_chain_hands_build_os_root_an_absolute_refer_path` 从 workflow 里**抽出那段 shell 本身**而不是重打一份，在临时工作区里跑它，再按模块的做法从树里 `stat` 结果。两个方向都量过：修好后 passed；退回 HEAD 后 `AssertionError: the refer path is workspace-relative ('ipsw-work/refer/iPad_Pro_HFR_17.7.11_21H461_Restore.ipsw')`。

**(5) 这个测试的第一版没有牙齿，而坏法值得记。** 它要求 `[ ! -f "$refer" ]` 那行守卫的确切拼写，于是未修复的 workflow 上以 `StopIteration` 失败——证明的是抽取器很脆，与缺陷无关。改成两种形状都抽（有或没有绝对化）之后，失败才落在缺陷本身。这与 11.7(5) 是同一类错误：**一个测试因为自己的前提不满足而红，和因为它守的契约被破坏而红，是两回事**，而前者比没有测试更危险，因为它看起来在工作。

### 11.10 本轮新增的未验证项（不得冒充的部分）

- **`os_root_status=pass` 与 `qemu_root_defconfig` 内核：仍未取得。** run 40203832707 的 step 14 成功但未发出 `pass`，step 15/16 因此被跳过。链的十个子步里没有更多未跑过的步骤（step 10 是最后一步），所以路径修好之后这一遍应当直接给出 `pass`——**但这是推断，不是测量**，验证它的 run 尚未触发。
- **`XNU_OWNER_DATA_VOLUME_UUID` 在容器从 32 MiB 长到 1 GiB 之后是否仍然有效：未经核对。** `prepare-owner-data-fixture.py` 写下的 `seed.json` 记的是 32 MiB 容器的 UUID（本轮实测 `18AF0FFB-77A7-4878-B999-70DD8B4076DC`），而 1 GiB 容器是之后由 `packed_ramdisk.py` 产出的；`build-macos.sh:1959` 从 `seed.json` 读 UUID 并导出。若增长过程换了 UUID，内核会嵌入一个过时的值——下一遍的日志（`build-macos.sh:1873`）会把它打出来，届时核对，现在不下结论。
- **本机 QEMU 上的根卷挂载与 launchd PID 1：仍为零。** 见 11.8，这一轮没有推进它，因为前置物（能读根的 `qemu_root_defconfig` 内核）还没有造出来。

### 11.11 第五轮：根链拿到 `os_root_status=pass`，第二遍构建退到它自己的第十个 archive（事实 51，2026-10-25）

**(1) refer 路径修好之后，链收口了，而见证 `pass` 的不是链条自己。** run 40206274286：step 14 success，**step 15 `Remove the root chain's disk volumes` 真的跑了**（`7.9G .../target-volumes` → `removed the OS and SystemOS volumes`）。这一步的 `if` 是 `steps.os_root.outputs.os_root_status == 'pass'`，所以它是 `pass` 的第一个独立见证。第二遍随之选中 `qemu_root_defconfig`、导出 `XNU_OWNER_DATA_VOLUME_UUID`、加上 `CONFIG_XNU_BUILD_ARCHIVES=y`，并建出 10 个 archive 里的前 9 个。

**(2) 第十个 archive 退出，而它缺的文件 `unixnu ipsw fetch` 本该留下。** `The original kernelcache is not in the IPSW cache; run "unixnu ipsw fetch"`。直接原因：本 workflow 自己解出并解压 kernelcache，却从不把它放进 `out/ipsw/<build>/`。

**(3) 而这一轮真正的新知识是第二层：这个洞恰好被两遍构建的顺序掩藏着。** `build-platform-power.py` 在没有参考内核时链接一个 no-op `sheng_pmgr_run` 并 `exit 0`，根本走不到那行检查。所以第一遍必然通过，第二遍（已有 `kernel/kernel.macho`）才是第一次需要那个文件。**把构建遍数从一遍改成两遍（事实 45）不只是让链能跑，它还让第十个 archive 第一次被真正执行到。** 一个此前每一遍都"通过"的检查，第一次被执行就失败了——这与本仓库反复出现的"一个声明在任何一个地方的依赖"是同一类：通过不等于被执行。

**(4) 修的时候抓到一个 PRD 12 的真问题，而它今天没有造成后果只靠步骤顺序。** `out/ipsw/` 在 `BUILD_OUTPUT_DIR` 里面，staging 会扫到它；`ROLE_RULES` 把 `kernelcache` 映射到**被允许**的 role `kernel`，`PRIVATE_PATTERNS` 里没有 `*.decompressed`——默认拒绝会凭名字放一个 Apple 私有二进制过关。今天它没被上传，只因为删 `out/ipsw/` 的步骤恰好排在 staging 前面。已补 `*.decompressed`，并用一个真的跑 staging 自己代码的测试钉住：删掉 pattern 再跑，测试打印出 `staged=[... 'unixnu-out/ipsw/21H461/kernelcache.decompressed' ...]`。

**(5) 这个测试的第一版又红在自己的消息里，这是同一类错误第三次。** 它引用了一个已被替换掉的 `manifest` 变量，于是删掉 pattern 之后以 `NameError` 失败——红，但红的理由不对。前两次是 11.7(5) 的 `apply_submodule_divergences()` 匹配到定义、11.9(5) 的 `StopIteration`。**一个测试因为自己的前提不满足而红，和因为它守的契约被破坏而红，是两回事**，而前者比没有测试更危险。三次都已把教训写进各自的注释，但这条值得单独记：写"证明它有牙齿"的那一步时，必须真的去看失败信息指的是不是那个契约。

### 11.12 本轮新增的未验证项（不得冒充的部分）

- **`qemu_root_defconfig` 内核：仍未构建出来。** run 40206274286 的 step 16 以 `stage build-kernel failed` 结束，`root_kernel` 未置位。`--kernelcache-cache` 的修法已推送，但**验证它的 run 尚未触发**，所以"第二遍现在能过"是推断，不是测量。
- **本机 QEMU 上的根卷挂载与 launchd PID 1：仍为零。** 见 11.8 / 11.10，这一轮没有推进它，因为前置物还没有造出来。
- **`XNU_OWNER_DATA_VOLUME_UUID` 在容器从 32 MiB 长到 1 GiB 之后是否仍然有效：仍未核对。** 见 11.10。`build-macos.sh:1873` 会把它打进日志，下一遍核对。
- **`build-platform-power.py` 这一次是真的执行了审计，还是仍然走了 no-op 桥：未经核实。** run 40206274286 里它死在第 50 行的检查上，这说明它**越过了** no-op 桥（否则不会到达那里），所以参考内核确实存在；但 `audit-amfi-pmgr-contract.py` 本身是否通过，要等内核cache就位的那一遍才知道。

### 11.13 第六轮：第二遍构建死在它自己的第十个 archive，根因是 `unixnu` 每次都会把子模块重置回 gitlink（事实 52，2026-10-26）

**(1) `--kernelcache-cache` 生效了，而这是可证的而不是推断的。** run 40209085545：链再次 `os_root_status=pass`（step 15 再次真的删掉两个卷），第二遍选中 `qemu_root_defconfig`、导出 UUID、建出前 9 个 archive。archive 10/10 `build-platform-power.py` **越过了**上一轮死掉的那行——它打印出了下一步 `+ bash .../build-xnu-4k-baseline.sh`，说明它读到了 `out/ipsw/21H461/kernelcache.decompressed`。

**(2) 然后死在 `build-xnu-4k-baseline.sh:735`**：`Rebuild the owned Legacy archive after changing drivers/NativeMobileDisplay/NativeLegacyDisplay.cpp`。

**(3) 根因是驱动自己的一个行为，而它把洞掩藏了两轮。** `cli.py:79-86` 的 `prepare()` 第一句是 `feeds.update_submodules(ctx)`，后者跑 `git submodule update --init --recursive`——把**每一个**模块子模块重置到驱动自己 gitlink 记录的提交。所以链在 `eb9c949` 建好的 Legacy archive 与第二遍 `unixnu build kernel` 之后的源不再一致。**第一遍里 archive 与源在同一次调用内产生，必然一致；第二遍才第一次出现「archive 是上一轮建的、源是这一轮重置的」这个组合。** 事实 45 的「跑两遍」让链第一次能跑，也让第十个 archive 第一次真正执行到（事实 51），并让子模块重置第一次有机会破坏一个已经建好的 archive——**一个修法的副作用要到两轮之后才显形**。

**(4) 修法是把「应用锁里的子模块提交」收敛成唯一实现，并放在每次驱动调用之后。** 新增 `src/unipad/host/source_pins.py`；`build-macos.sh` 的 `run_driver` 在驱动返回后调用它，不管子命令是什么——因为枚举「哪些子命令会重置」本身就是又一个要靠记住的事。workflow 的 acquisition 步骤改为调用同一个模块：两份「哪个提交」正是本仓库反复踩的第二个知道 sha 的地方。

**(5) 重构本地就抓到两个我自己的缺陷，都在推到 runner 之前。** (a) 删旧函数体时把 `moved = []` 一起删了；(b) acquisition 测试期望的是**步骤自己的** `SourcePinError`，而模块抛自己的——修法是把模块的 `UniPadError` 包成步骤的类型，因为步骤对调用方的契约是它自己的类型。**这两个都是「重构一段能跑的代码」的典型代价，而它们是那个行为测试抓到的，不是 code review 抓到的**——这再次说明 `test_acquire_submodule_divergences.py` 跑步骤自己的代码而不是读它，是对的。

**(6) 新测试也抓到我自己的一个缺陷。** 禁用列表里写了 `submodule`，而函数名就叫 `pin_module_submodules`——它以自己的名字失败。这与 11.7(5)、11.9(5)、11.11(5) 是同一类错误**第四次**。四次分别是：匹配到函数定义、`StopIteration`、消息里的 `NameError`、禁用列表里的函数名。**共同点很一致：写「证明它有牙齿」的那一步时，没有真的去看失败信息指的是不是那个契约。**

### 11.14 本轮新增的未验证项（不得冒充的部分）

- **`qemu_root_defconfig` 内核：仍未构建出来，这是连续第三轮。** run 40209085545 的 step 16 以 `stage build-kernel failed` 结束。三次死因各不相同（相对 refer 路径、kernelcache 缓存缺失、子模块重置），每一次都由前一次的修法暴露出来；**「下一次能过」始终是推断**，验证它的 run 尚未触发。
- **本机 QEMU 上的根卷挂载与 launchd PID 1：仍为零。** 见 11.8 / 11.10 / 11.12。
- **`XNU_OWNER_DATA_VOLUME_UUID` 在 32 MiB → 1 GiB 之后是否仍然有效：仍未核对。** 见 11.10。三轮了，每次都被下一个阻断挤掉；`build-macos.sh:1873` 会把它打进日志，内核真正建出来的那一遍必须核对。
- **run 40214067252 暴露的 import 缺陷：修法与测试都已于本轮之前补上，但尚未在 runner 上验证。** 步骤现在自己把 `src` 放进 `sys.path`，而为它写的测试用子进程 + `-S` 复现了 runner 上那条 `ModuleNotFoundError`。**仍未测量**：一次真的 acquisition。
- **`run_driver` 里的 re-pin 在真 runner 上的开销未测量。** 每个驱动调用多 21 次 `git rev-parse`（锁里的子模块数），只有真发生分歧的才 fetch。设计上是幂等且廉价的，但没有量过。

### 11.15 第七轮：re-pin 放错了位置，而定位它的是同一个 log 里的两个时间戳（事实 53，2026-10-26）

**(1) 上一轮的修法被 runner 判为「工作正常但时机错误」。** run 40217489782：链再次 `os_root_status=pass`，第二遍再次死在 `build-xnu-4k-baseline.sh:735`。而 log 同时打印了 `17:50:35.288 Rebuild the owned Legacy archive after changing ...`（链接脚本拒绝）与 `17:50:36.310 pins: drivers/NativeMobileDisplay: c031d882 -> eb9c949`（re-pin 正确修好了）。**后者比前者晚一秒。**

**(2) 根因是作用时刻，不是作用位置。** `cli.py:79-86` 的 `prepare()` 第一句就是 `feeds.update_submodules`，跑 `git submodule update --init --recursive`——重置是驱动调用的**第一个**动作，所以这次调用里建的 10 个 archive 与链接全部看到 gitlink 的提交。驱动返回之后修什么都已经晚了。**这是被两个时间戳定位的，不是被推理定位的**，而它推翻了上一轮「放这里就行」的判断。

**(3) 修法是让执行重置的那条命令自己跳过。** `submodule.<path>.update=none`。本机对真的 `vendor/UniXNU` 量了三个事实：不设它 → `Submodule path 'drivers/NativeMobileDisplay': checked out 'c031d882...'`（确实重置）；设了它 → `Skipping submodule 'drivers/NativeMobileDisplay'`（检出不动）；`--init` 跟着被跳过（删掉目录再跑不会重新克隆），**所以保护必须在子模块落盘之后才设**——本 workflow 的顺序正是如此。它写在 `.git/config`：本地状态而非提交，在每次重新克隆的 runner 上依然有效。

**(4) 为它写的测试经历了三轮才真的有牙齿，每一轮都是一种新的假绿。** (1) pytest 进程里删 `PYTHONPATH` 无效（`src` 在本机 `sys.path` 上，`sys.modules` 直接给答案）→ 子进程 + `-S` 才复现 runner 的 `ModuleNotFoundError`。(2) 合成工作区只有 clone 没有 `.gitmodules`，`submodule update` 是空操作，删掉保护照样过 → 补 `.gitmodules` 与 gitlink。(3) 补上后撞 `fatal: transport 'file' not allowed`，退出 128 而检出没动，**又是假绿** → 设 `protocol.file.allow=always`（驱动与子模块两处）才拿到契约本身的失败。**至此四种假红加三种假绿，共同点是没有真的去看失败信息指的是不是那个契约，也没有真的确认被抵抗的动作真的发生了。**

### 11.16 本轮新增的未验证项（不得冒充的部分）

- **`qemu_root_defconfig` 内核：仍未构建出来，连续第四轮。** 四次死因各不相同（相对 refer 路径、kernelcache 缓存缺失、子模块重置、re-pin 时机）。`update=none` 的修法已推送，**验证它的 run 尚未触发**。
- **`update=none` 是否会被 runner 上某些 git 版本或 driver 行为绕过：未测量。** 它依赖 git 对 `submodule.<path>.update` 的既有语义（`git help submodule`），本机 git 已量；runner 的 git 版本不同，未量。
- **`--init` 被跳过这一语义的后果：未在任何真 runner 上测量。** 本机已量（缺失的子模块不会被重新克隆）。本 workflow 的顺序保证子模块先落盘，但如果将来有人把保护移到 `unixnu fetch` 之前，会得到「子模块永不初始化」——这一点只有注释与测试守着，没有门禁。
- **本机 QEMU 上的根卷挂载与 launchd PID 1：仍为零。** 见 11.8 / 11.10 / 11.12 / 11.14。

### 11.17 第八轮：第一次走到内核链接，死在一个本板没有的按钮驱动符号上（事实 54，2026-10-26）

**(1) 子模块保护生效了，而且这一次越过了此前每一个阻断。** run 40222638377：archive 10/10 `build-platform-power.py` 越过，`build-xnu-4k-baseline.sh` 第一次真的去链接内核——**这个项目第一次走到内核链接**——然后 `Undefined symbols for architecture arm64: "_native_buttons_set_sink", referenced from: NativeHidInput::start(IOService*) in libNativeLegacyDisplay.late.a[14](NativeHidInput.late.o)`。

**(2) 为什么以前没见过。** `qemu_root_defconfig` 与 `qemu_defconfig` 的唯一显示差别是 `CONFIG_XNU_NATIVE_LEGACY_DISPLAY`，它用 `-Wl,-force_load` 把整个显示 archive 拉进来——所以 `NativeHidInput.late.o` 是第一次被链接进任何内核。

**(3) 根因不是声明写法，而对照就在同一个文件里。** `native_buttons_set_sink` 声明为 `weak_import` 且每次调用前用 `&symbol` 守，是标准写法；同一文件第 13 行的 `native_touch_set_sink` 写法完全一样却不报错——**差别只有一个：它在设备仓库 `package/TouchState.cpp:23` 有定义**。同一 run 里还有一组对照：`NativeRamBlock.cpp` 的五个 `native_ufs_store_*` 也是 `weak_import`、同样没有定义、在普通 `.o` 里却链接通过。所以可观测事实是：未定义的弱引用在普通对象里是 null，在 `llvm-objcopy` staging 过的 `.late.o` 里是硬错误。**这条因果是当时证据最强的解释，不是已证实的机制**——`llvm-objcopy` 是否真的丢掉 Mach-O 的弱标记，没有直接测量。

**(4) 处置，以及为什么它只能是空实现。** 本板没有硬件按钮驱动，可选 sink 按设计缺席；空实现与缺席行为等价。而这是本仓库唯一抓得动的杠杆：声明与 staging 都在被钉死且每次重新克隆的驱动仓库里，本地编辑活不过一个 run。实现走 `XNU_EXTRA_LD_ARCHIVES`——`build-xnu-4k-baseline.sh:732` 本来就把它并进内核的 archive 列表，正是为这种形状留的接口。

### 11.18 本轮新增的未验证项（不得冒充的部分）

- ~~**`qemu_root_defconfig` 内核：仍未构建出来，连续第五轮。**~~ **〔2026-10-26 已闭合：run 40241617618 报 `stages: ran=7 not_run=0 failed=0 skipped=1`，workflow 自己的回读确认 `the kernel was built from qemu_root_defconfig`。这是本项目第一次构建出带 owner root chain 的内核。最后一轮的修法与内核无关——是一个 heredoc 里的 `
`（见 11.19）。〕**
- **`llvm-objcopy` 丢掉 Mach-O 弱标记：未证实。** 有三组对照证据（同文件另一个可选符号有定义所以不报、五个 `native_ufs_store_*` 在普通 `.o` 里不报、`LegacyDisplayProbe.o` 未 objcopy 所以不报），但没有一次是直接测量 objcopy 前后符号表的弱位。如实记为「当时证据最强的解释」。**〔2026-10-26 补：run 40241617618 链接已过，所以这条机制即使为真也不再承重；它仍然是未测量的。〕**
- **空实现是否会被 `NativeHidInput` 真正调用：未测量。** 设计上会（`&symbol` 现在非空），而空体意味着什么都不发生。要等内核跑起来才能量。
- **run 40238050858 暴露的 heredoc 缺陷：已修并已有编译门禁，但未在 runner 上复验。** `build-macos.sh` 里喂给解释器的每个 heredoc 现在都会被真编译（`test_build_macos_python_syntax.py`），而该门禁本身只在本机量过。**〔2026-10-26 已由 run 40241617618 复验：该 run 的第二遍构建没有再现 SyntaxError，且走完了 7 个 stage。〕**
- **本机 QEMU 上的根卷挂载与 launchd PID 1：仍为零。** 见 11.8 / 11.10 / 11.12 / 11.14 / 11.16。

### 11.19 run 40241617618：内核第一次构建出来，同时暴露出下一层缺口

这一轮的两个事实必须分开记，因为它们一个已证实、一个没有。

**(1) 已证实。** `qemu_root_defconfig` 的内核构建出来了。第二遍构建 `stages: ran=7 not_run=0 failed=0 skipped=1`，`failed=0`；workflow 的回读（`grep -q 'building qemu_root_defconfig'`）确认内核来自 root config。artifact `boot-inputs-40241617618`（260,410,178 字节）已下载到本机。

**(2) 未证实，且是下一层的缺口。** 同一个 run 的日志写着 `payload: no trust cache in the volume (board.toml allows 2)`——而这个 stage 是**成功**的。原因是 `payload.py:99-101`：`trust` 在两个 flag 都缺席时是 `None`，随后 `if trust:` 包住整个校验块且**没有 else**，于是拷贝跳过、`make-qemu-adt.py` 不被告知、payload.json 记 null，卷照写、退出码 0。`build-os-root.py:26-27` 写着代价（AMFI 拒绝 launchd，namespace 9 / EXEC / code 8 / SECURITY_POLICY），同一个脚本的最后一行就是该跑的命令。**所以「payload 阶段从未传过 `--trust-cache` / `--data-ramdisk`」这个缺口此前被构建成功完全掩盖**，而它的形状让它看起来像进展：第二遍围着「终于能挂根卷的内核」重建 ESP，缺这两个 flag 让构建更接近完成、让访客更不可能起来。

**(3) 已做但未在 runner 复验。** `build-macos.sh` 新增两个 flag 并由 `step_build_payload` 转发，workflow 第二遍从 chain 的产出路径取文件传入。测试 `tests/acceptance/test_payload_trust_cache.py` **执行** payload 阶段的 argv 构造区段（stub 掉 `run_driver` 后断言它收到的参数），并执行 workflow 第二遍的 shell（stub 掉 `build-macos.sh` 后断言它收到的参数）。八项反向删除全部验证为红，其中一项的边界如实记录在该测试自己的 docstring 里。**「传进去之后 AMFI 放行 launchd」是设计意图，不是实测。**

### 11.20 外部阻断：GitHub Actions 计费失败，runner 验证暂停

2026-10-26，提交 `34fe029` 之后，`preflight` 与随后手动触发的 `build-macos` **都没有启动任何 job**，两者的 annotation 是同一句：

> The job was not started because recent account payments have failed or your spending limit needs to be increased. Please check the 'Billing & plans' section in your settings

**这不是代码问题，也无法在本仓库内修复。** 它挡住的是「runner 验证」这一栏，不是「代码完成」这一栏：

- 已代码完成并有本机测试的：trust cache / Data ramdisk 转发（提交 `34fe029`，8 项反向删除全红）、probe 的 `--inputs` / `--trust-cache` / `--devicetree`。
- **未在 runner 上复验的**：`34fe029` 之后没有任何 run 启动过，所以「传进去之后 AMFI 放行 launchd」仍是设计意图。上一个成功的 runner 证据仍是 `40241617618`（`qemu_root_defconfig` 第一次构建出来）。
- 本机侧未受影响：`qemu_root_defconfig` 内核、OS 卷挂载、PID 1 的实测证据全部来自本机 QEMU TCG，不依赖 runner。

**处置**：不因此停下本机工作。恢复条件是用户结清 GitHub 计费或提高 spending limit；恢复后第一件事是重跑 `build-macos`（stage=M3）验 `34fe029`。


### 11.21 仓库已转公开，Actions 因此恢复

2026-10-26，用户指示「将仓库转公开，私有额度用完了」。**转公开之前先做了审计**，因为这一步不可逆（fork、镜像、搜索引擎索引在重新转私之后依然存在），而 PRD 12 与项目自己的规则都写着绝不发布 Apple 二进制、私钥、个人 Data 或其派生物：

- 205 个被跟踪文件，除一个 70 KiB 的合成测试 ELF（`tests/fixtures/arm64/boot-stub.elf`，本仓库测试自建）之外全是 UTF-8 文本；
- `local-private/` 下 **0** 个被跟踪文件；
- 树里与全部 113 个提交里，Apple 二进制、密钥、证书、seed **0** 个；`.git` 只有 4.2 MiB；
- 最近 50 个提交里搜不到本机用户名。

**审计中发现一个真实隐患并先修掉再转**：`/vendor` 只写在 `.git/info/exclude` 里——那是本地状态，克隆不继承，别处一次 `git add -A` 就会把整个被钉死的 checkout stage 进去。而 `vendor/UniXNU/refer` 是一个指向 `local-private/ipsw` 的 junction，**一个 6.8 GiB 的 Apple 恢复镜像因此就在工作树里、一个 glob 就能够到**，挡着它的是一条只有本机有的规则。提交 `b0ee427` 把 `/vendor` 移进提交内的 `.gitignore`，转公开之后 `git add -A --dry-run` 只会有文档文件。

**转公开的直接后果：Actions 恢复。** 公开仓库的 Actions 计算是免费的，11.20 记的计费失败随之消失——随后手动触发的 `build-macos`（run 40250055465）正常启动并进入 in_progress。**所以 11.20 的「runner 验证暂停」已解除**，`34fe029` 的 trust cache 转发终于可以复验。


## 11. 结论与下一步

代码阶段：**该做的都做完了**，包括本轮为产出内核镜像而写的 macOS 构建管线，以及它自己暴露并修掉的两个真实缺陷。

**CI：两个 job 从 2026-10-19 起每次 push 都红，本轮修好并在真 runner 上验证（run 40170192535，两个 job 全 success）。** 两个红都不是代码缺陷而是缺依赖声明：`tests/integration` 从来没有在 CI 上跑过一次（job 证明了 pin，然后在没有 pytest 的裸解释器上跑收集），`tests/unit` 收集不起来（`test_workflow_contracts.py` 在模块级 import `yaml`）。修的过程中还抓到自己引入的第二个缺陷——三个新测试用 `vendor/UniXNU-qemu`，而它是 git-excluded 的，unit 阶段跑在它不存在的 checkout 里；改成现搭合成 board，67 tests / 0 failures / **0 skipped**。**这一条是"代码完成 + CI 验证"，不是"验收通过"**：它证明的是门禁自己会跑，不证明 iPadOS 用户态能起来。

真机阶段：**BLOCKED**，阻断项见第 6 节，全部指向同一件事——需要一个 macOS runner（或用户自有的 Mac）跑一次 `.github/workflows/build-macos.yml`。跑通之后，M0 的 `clean_build_artifacts` 缺口闭合，M2–M4/WP02–WP04 的 `built_unixnu_kernel_image` 根因才有可能消失，M5 之后的链条才第一次有可观察的对象。

**而"跑一次"具体缺什么，本轮查清了（见 11.5(2)）**：不是缺 runner，是 runner 上 `build-os-root` 永远拿不到本地 IPSW——workflow 只调用一次 `build-macos.sh`、从不传 `--ipsw`，它自己的 IPSW 步骤取完 kernelcache 就把 6.8 GB 镜像删了。于是无链、内核恒 `qemu_defconfig`、第二个块设备永不被枚举。修法候选（保留 IPSW 并在链不再需要时删掉 / 同一 job 内跑两遍 / 只留链需要的成员）已派 teammate 评估，空间约束是 8 GiB 地板对约 14 GiB 空闲，不能靠抬高地板绕过。**在修好并真跑过一次之前，任何"根卷可挂载""launchd 已启动"的说法都是不诚实的。**

在跑那一次之前，任何"P0/P1 验收通过"的说法都是不诚实的。
