# ADR-0011: 客人侧代理（Guest Agent）——它在哪里运行，以及它不解决什么

- 编号：ADR-0011
- 日期：2026-10-31
- 状态：proposed (code stage) —— 宿主侧脚手架已采纳；**客人侧实现 UNVERIFIED / BLOCKED（本机没有任何客人进程运行过，连「枚举到设备」都没有）**
- 关联需求：PRD §2.3 H03/H06、§3.1 FR06、§5.2 用例 3–4/10、§6 M10/M10a/M11/M12、§9.1 安装接口契约、§9.2（无账号/令牌/用户文档）、§10（故障分类封闭集）
- 关联契约：`unipad.guest.protocol`（`install/launch/persist/status/challenge` 五对消息）、`unipad.guest.client.GuestClient`、`unipad.guest.agent_channel`（六态与「一次连接」规则）、`unipad.guest.agent_stub.GuestAgentStub`（本 ADR 新增的宿主侧对端）、`unipad.errors.ErrorCode.{INSTALL_SERVICE_UNAVAILABLE, REGISTRATION_FAILED, TRANSFER_FAILED, SIGNATURE_OR_POLICY_REJECTED, PERSISTENCE_FAILED}`

> **编号说明：** 任务描述里写的是 `ADR-0011-guest-agent.md`，0011 是空号（现有到 ADR-0010），直接采用，无需改号。

## Context

1. **宿主侧已经齐了，客人侧是空的。** ADR-0003 立了协议，ADR-0010 补了 `launch` / `persist` 两对消息，`agent_channel.py` 立了第一个对端是 QEMU 设备的传输层。但**没有任何东西在 iPadOS 里读那个 virtio-serial 端口**：`FakeGuestService` 是唯一的协议实现，而它是测试替身。四环（安装/注册/启动/持久化）在客人侧全部断在「无人应答」。
2. **事实 197 把「宿主自己动手」这条路彻底关了，于是客人侧从「需要」变成「唯一」。** 实测 QEMU 2026.8.11 的 32 个 object type 里 hostmem 只有 `memory-backend-ram`，`memory-backend-file` / `-memfd` 均 `invalid object type` 退出 1（反向对照 `memory-backend-ram` 被接受，所以这是答案不是没测到）；成因是 Windows 移植的 `CONFIG_POSIX` 构建配置，**任何官方 w64 构建都不会有**，「等一个新 QEMU」从此不是可推进选项。同一轮量到 QMP 238 条命令里与内存相关的全是读（`pmemsave` / `memsave` / `dump-guest-memory` / `screendump` / `snapshot-save`），**没有一条能写客人物理内存**；gdbstub 能写却只在 MMU 关闭时可达（事实 194），而真启动里 MMU 是开的。**结论：`_agent_argv()` 的 virtio-serial socket 是唯一一条宿主→客人可写通道，而它需要客人侧实现。**
3. **这条通道的实测状态是 `silent`，不是 `answered`。** `agent_channel.py` 的 docstring 记录了原因：`NativeVirtioConsole` 不在本板构建里（`board.toml` 的 archives 直到 fork 的 `acf2f86` 才点名它，且此后没有 macOS runner 构建过它），所以内核在该传输上枚举不到设备；QEMU 仍然接受 TCP 连接，因为接受 TCP 是 chardev 的职责，virtio-serial 设备是否存在与客人有没有驱动无关。**「连上了」因此一文不值，而把它当信号用会永远报告客人可达。**
4. **这条通道一次连接，这让「客人起得晚」变成危险路径。** chardev 在一个 QEMU 进程的生命周期里只服务**一个** TCP 连接（实测 `chardev-connection-count.py`：第一个被接受，此后每一个——包括第一个还被握着时发起的——都被立即且永久地以 `ECONNREFUSED` 拒绝）。所以一个探活、超时、关闭、再探活的客户端会把 boot 唯一的那次连接花在一次问题上，然后读到 `refused` 并断定 boot 没了。`AgentChannelTransport` 因此**在超时后保留 socket**。
5. **「客人服务起得晚」因此不是一个边缘情形，而是今天唯一可预期的情形。** 没有任何证据表明客人会在宿主连接时就已经把服务拉起来了。`FakeGuestService` 建模不了这件事：它要么在（立刻应答），要么根本不存在（没有监听）。这就是本 ADR 要补的那块。
6. **存在一条被 ADR-0003 判死的捷径，而它在「放一个代理进去」这个动作上更容易走。** 把 `.app` 预置进 Data seed / 镜像，图标会出现在桌面上，`launch` / `persist` 之后四环看起来全通了。ADR-0003 决定 4 与 ADR-0010 决定 6 都明确**只修改 Data seed 的预装方案不通过 FR06**。本 ADR 要在「代理本身怎么进去」这个问题上把这条线再划一次，而且要比 ADR-0003 更细：**基础设施进 seed 与「被导入的 App」进 seed 是两件事，前者是让后者可被测试的前提，后者才是 FR06 问的东西。**

## Decision

1. **先定「代理长什么样」，不选「它怎么进去」。** 通道选择 ADR-0003 已经推迟到 M10 实验，本 ADR 不提前收窄。但三种候选放置方式的共同前提是同一个东西，所以先把它定死：**客人侧代理是一个单一、小巧、无 GUI 的常驻进程，唯一职责是打开 virtio-serial 端口、按 `unipad.guest.protocol` 说 JSON Lines、把 install/launch/persist/status/challenge 五对消息分派给后面的安装链。** 它不解析 IPA（包预检是宿主 `unipad.ipa` 的职责，ADR-0003 的证据表把「包预检报告」列为宿主产物），不持账号、不持令牌、不读用户文档（PRD §9.2），不自己发明错误码（PRD §10 是封闭集）。这个形状在三种放置方式下都不需要改，所以它是可以现在定的那一部分。
2. **三种放置方式的诚实评估，逐条说明 Data seed 能交付哪个、不能交付哪个：**

   | 方式 | Data seed 能交付的 | 不能交付的 | 是否打破 bootstrap 循环 |
   | --- | --- | --- | --- |
   | (a) Data seed 里的 launchd 守护进程 | 二进制与 plist **文件**：宿主本来就在构建 Data 镜像（ADR-0002），多放两个文件是镜像构建步骤，不是客人运行时安装 | **让 launchd 真的加载它**：launchd 的作业搜索路径在系统卷上，一块独立挂上的 virtio-blk 数据盘不自动成为搜索路径（ADR-0002 决定 1 把 Data 做成独立可写盘，决定 4 把应用安装产物放 Data 卷）。把 plist 放到 Data 卷**不等于** launchd 会读它 | **不打破**：文件进得去，执行进不去，除非改系统卷——而那会让它坍缩成 (c) |
   | (b) 宿主先装一个「安装器 App」，由它再装别的 | 同 (a)：只能预置一个 App 的**文件** | **App 装 App 的能力本身**：那需要安装服务，也就是要先有代理 | **不打破**：它把循环往后挪了一环，没有断开。「先装安装器」和「先有代理」是同一个前提 |
   | (c) 内核级 / 早期用户空间钩子（改 XNU 或启动链，让端口在 launchd 之外被打开） | —— | 需要一次客人镜像构建；本机没有 macOS 构建器（`NativeVirtioConsole` 不在本板构建里，见 `agent_channel.py`），唯一构建发生在 runner 上 | **打破**：这是唯一不依赖 launchd 搜索路径、也不依赖「先装一个安装器」的方式 |

   结论写在这里以免被误读：**(c) 是唯一真正断开 bootstrap 循环的方式；(a) 交付文件而不交付执行；(b) 预设了循环已断。** 三者都不是现在能选的——ADR-0003 已经把通道选择推迟到 M10，本 ADR 只提供选择时需要的判据，不代替实验。
3. **FR06 的线划在「每次导入要不要重做镜像」，不划在「客人构建过一次」。** FR06 的措辞是「导入新 App 不应要求重新编译 XNU 或重新生成整套系统镜像」。按字面读，它约束的是**每次导入**的代价，不是客人被构建过一次。所以：(i) 一次性把代理构建进客人镜像，然后运行时导入任意 IPA 而**不再**动镜像 —— 这在 FR06 的右边；(ii) 每导入一个新 App 都要重编 XNU 或重生成镜像 —— 这在左边。**一个预置在 seed 里的代理本身不满足 FR06，也永远不得被记录为满足**：它只是让那个能判定 FR06 的实验有东西可应答。判别方法仍然只有 ADR-0010 决定 6 那一条，且不依赖任何新消息：**桌面启动后导入一个此前未安装的 IPA，且该导入不要求重编 XNU、不重新生成整套系统镜像。**
4. **真实 iPadOS 客体上「安装」必须做四件事，本 ADR 逐件标注证据等级，并且不发明 API 名：**

   | 步骤 | 本机证据 | 本 ADR 的表述 |
   | --- | --- | --- |
   | 建立应用容器 | **无** | 推断：每个 App 有独立容器；具体机制不是公开 API。这里**不写任何私有 API 名**——写一个听起来对的名字比不写更糟，因为它会被当成已知事实引用 |
   | 向安装守护进程注册 | **无** | 推断：存在一个操作系统 daemon 拥有应用安装与注册。它的名字在公开的第三方逆向材料里出现，**本机未验证，且本设计不依赖该名字**——协议只依赖「有一个注册记录被建立」这个事实，由 `install.response.stage='registered'` 上报 |
   | 放置图标 | **部分（仅存在性）** | 事实 191 的 70 分钟窗口采样到客人进程表里有 `SpringBoard`（`opens=1`、`refusals=1` 同轮量到），这证明**主屏幕层存在**；**图标如何被注册完全没量过**。不得把「有 SpringBoard」推成「放图标可行」 |
   | 记录信任方式 | **无** | `trust_mode` 必须是客体**实际**做出的判定，协议侧早已是「记录而非假定」（`protocol.py` / ADR-0003 决定 3）。在未安装受信根证书的客体上，自签名包被拒绝是**合理预期**，这是 M10a 的真实风险，不是可以跳过的步骤 |

   **宿主在这些事上一件都做不了**：事实 197 已把「宿主写客人物理内存」量成定论（QMP 238 条无此命令，官方 w64 构建永不会有 `memory-backend-file`）。所以上表四步的执行者只能是客人侧代理，这也正是 ADR-0003 把 `INSTALL_SERVICE_UNAVAILABLE` / `REGISTRATION_FAILED` 标为「分类就绪，不算已支持」的原因。
5. **代理的应答纪律与 `agent_channel` 的六态对齐，尤其是「未就绪」这一态。** 代理必须在能应答之前**接受 TCP 连接但什么都不说**——因为那是本机今天已测量的状态（`silent`），也是一个只在就绪后才开始监听的 stub 会掩盖掉的失效模式。由此产生一个只有客人侧实现者才会遇到、而宿主侧必须替它先想好的决定：**就绪前到达的请求怎么办。** `unipad.guest.agent_stub` 把两种政策都实现并钉住：
   - `STALE_DISCARD`（默认）：读进来、记下来、**不回答**。理由是一条迟到的应答会占掉下一个问题的回复位——见下一节。
   - `STALE_ANSWER`：排队，就绪后按到达顺序回答。本 ADR 明确**不推荐**它，并用测试钉住它会造成的后果。
6. **迟到的应答不是无害的，它会让下一个问题读到一个不属于它的回复。** `AgentChannelTransport.request` 读取**恰好一行**并返回它解析到的东西，它**不**校验 `id`；`id` 关联只存在于 `GuestClient._validate_reply` 里。所以一个在预算过期后才回答旧问题的代理，会把一条没人等待的回复留在流里，下一个调用方把它当成自己的答案。已在 `tests/unit/test_guest_agent_stub.py::test_answering_a_stale_request_consumes_the_next_questions_slot` 钉住：走 `STALE_ANSWER` 时，客户端拿到的是 `qmp_protocol_error`（`guest reply id does not match the request id`），而代理其实两个问题都答对了。**这是本 ADR 给客人侧实现者的最直接一条指令：就绪前收到的请求要丢弃，不要延迟回答。**
7. **`install.progress` 在这条通道上没有安全位置，需要它自己的决策记录。** `install.progress` 被定义为 guest → host，但 `GuestClient._exchange` 每个请求只读一行，于是一条插在 install 中间的进度消息会被当成应答读走并以 `guest reply type does not match the request` 拒绝。客人要么不要在这条连接上发进度（`GuestAgentStub` 的做法：记进自己的日志），要么要一次协议修订，要么要第二条连接——而「一次连接」规则（决定 5 的前提）禁止后者。**本 ADR 不修订词汇表**（ADR-0003 的修订程序要求独立决策记录），只把缺口记在这里。
8. **`GuestAgentStub` 是宿主侧脚手架，不是客人实现的替代品。** 它在 `src/unipad/guest/agent_stub.py`，是一个**服务端**：绑 loopback 端口、宿主连它、说同一条 JSON Lines、按请求的 `id` 回 `id`、按 ADR-0003 的阶段链走 install 并**拒绝上报没有走到的阶段**、支持按阶段注入故障（码逐字通过，且只接受 PRD §10 封闭集内的码，注入集外码是 `config_invalid`）、未就绪前拒绝应答任何东西、服务一个连接后关闭监听（复现实测的「一次连接」）。它存在的理由是：**在没有任何客人的机器上，把「客人起得晚」「客人永不起」「客人答非所问」这三种情形变成可在 Windows 上重复运行的测试**，而不是等到现场第一次连真机时才发现客户端在第三种情形下会把陈旧回复当答案。

## Consequences

**正面**

- 宿主侧四环可以第一次被**端到端**驱动：install → launch → persist 全在一条真实 socket 上跑完，且断言「只服务了一个连接」——一个每个动词重连一次的 stub 会让花掉 boot 唯一连接的宿主看起来是对的。
- 「客人起得晚」从不可测试变成可测试，且测试断言的是**同一条连接**被复用（`connections_served == 1`），这正是「一次连接」规则要求的性质。
- 故障注入按 ADR-0003 的**上报阶段**而非 PRD §10 的**链上断点**设键，与 `FakeGuestService` 的七点词表分开但共用同一协议；两个模型各自钉住自己要钉的那一侧，不会互相掩盖。
- 决定 6 把一条只会在现场暴露的失效模式（迟到应答污染流）提前变成了红测。

**负面 / 代价**

- **客人侧仍然完全是 UNVERIFIED / BLOCKED。** 本 ADR 一行代码都没让 iPadOS 多一个进程。已测量的状态是 `silent`；`NativeVirtioConsole` 不在本板构建里；没有任何 guest-side 代码在本机运行过。
- **三种放置方式一个都没选**，所以「代理怎么进去」这个问题原样留给 M10。这不是疏漏：选它需要现场证据（launchd 是否从 Data 卷读作业、镜像构建管线能否容纳一个常驻进程），而这两样本机都没有。
- **决定 4 的四个步骤全部是推断，除「SpringBoard 存在」这一条存在性证据。** 真实客体上的容器建立、注册、图标放置、信任记录都可能与本协议的字段形状不符；届时按 ADR-0003 的程序再立一份独立决策记录修订，**不得**在功能提交里顺手改。
- `GuestAgentStub` 与 `FakeGuestService` 有两个模型（ADR-0003 阶段词表 vs PRD §10 断点词表），需要各自保持与词表同步；已用 `test_a_failure_may_be_injected_at_every_stage_in_the_vocabulary` 与 `test_the_stub_never_reports_a_code_outside_the_taxonomy` 分别钉住。
- 本 ADR 记录了两个**宿主侧的既有缺陷**（见下一节），都不由本 ADR 修复，因为它们不在属主范围内。

**不做的事**

- 不把 `.app` 预置进 Data seed 冒充 FR06，也**不把代理预置进 seed 冒充 FR06**。
- 不写任何未在公开材料中出现、且本机未验证的 iPadOS 私有 API 名，更不把它当已知事实。
- 不修订 `install.progress` 的线上行为（那需要独立决策记录），只在决定 7 记缺口。
- 不宣称本机存在任何能回答 `install.request` 的客人。
- 不因为客人侧进不去就把 FR06 悄悄改成「预装即可」。

## Status

proposed (code stage)。代码阶段交付：`src/unipad/guest/agent_stub.py`（`GuestAgentStub` 与 `start_agent_stub`，含就绪门、`STALE_DISCARD` / `STALE_ANSWER` 两种迟到请求政策、按 ADR-0003 阶段注入故障、一次连接保真）、`tests/unit/test_guest_agent_stub.py`（28 项，全绿，三次重复无 flake）、变异检查 `local-private/tmp/mutate-agent-stub.py`（**12/12 全部被捕获**，含基线红则中止、锚点缺失记为 ANCHOR ERROR、CAUGHT 必须解析出失败测试 id 三条自律规则；恢复后按字节校验目标文件未被改动）。

变异检查第一轮有 1 个变异存活（M5：信任检查被跳过却无人发现——没有任何测试驱动过「策略拒绝」这条路径），修复方式是补 `test_a_package_the_trust_policy_refuses_is_never_installed`，而不是宣称该守卫已被覆盖。第一轮另有 2 个变异（M9/M10）因 `_weaken` 的锚点带上了 `if` 而产生 SyntaxError，被自律规则正确判为 INCONCLUSIVE 而非击杀——**那正是「退出码非零但没有失败测试行」的假阳性形态**，锚点修好后两个都变红。

**四环的诚实状态（与 ADR-0010 的表并列，不覆盖它）：**

| 环节 | 消息 | 状态 | 缺的那一件事 |
| --- | --- | --- | --- |
| 安装 | `install.request` | CODE COMPLETE（宿主侧 + 可驱动对端）；客人侧 UNVERIFIED | 真实客体安装服务应答一次 |
| 注册 | `install.response.stage='registered'` | CODE COMPLETE（宿主侧）；客人侧 UNVERIFIED | 真实客体确认容器/注册记录 |
| 启动 | `launch.request` | CODE COMPLETE（宿主侧）；客人侧 UNVERIFIED / BLOCKED | 真实客体确认入口执行（并给出 M11 挑战值） |
| 持久化 | `persist.request` | CODE COMPLETE（宿主侧）；客人侧 UNVERIFIED / BLOCKED | 真实客体刷写 Data 卷并报告，且模拟重启后回读一致 |

**本 ADR 期间量到的一个宿主侧缺陷，已被属主修复（记录过程，因为它的形状值得留档）：**

1. `tests/unit/test_guest_agent_channel.py::test_a_timeout_leaves_the_connection_usable_for_the_next_request` **曾在绿色时是因为错误的原因通过的**。本 ADR 工作期间用真实 `Listener` 复现其原序列 6 轮（`local-private/tmp/guestagent/probe_real_listener.py`）：**0 轮**拿到正确的回复（`id='b'`），3 轮超时，3 轮拿到的是**第一个请求的回复**（`id='a'`）。成因：`Listener._handle` 对**每一行**都先 `time.sleep(delay_s)`（0.8s），所以第二个请求的回复要到 `2*delay_s` 才存在，而第二个请求的预算只有 0.4s；测试在预算内收到的是第一条迟到回复，而它只断言 `reply["type"]`，不断言 `id`——`AgentChannelTransport.request` 也不校验 `id`（见 ADR-0011 决定 6）。该测试还在负载下约 1/3 概率 flake（本机复现：不带本 ADR 新增文件的 4 文件组合跑 3 次，第 2 次红在同一处）。**这与 ADR-0007「条件名与实现不一致」是同族缺陷：测试声称的性质没有被测到。**
   **该文件属主在同一轮里独立定位并修复了它**（`git diff tests/unit/test_guest_agent_channel.py`）：改为断言 `reply["id"] == "b"`（附言「a stale reply is not a successful retry」），并把 `Listener` 换成只延迟到一个截止时间的 `BecomesReady` 助手。修复后本机三连跑全绿。**两条线各自量到了同一个缺陷，这是对缺陷真实性的独立确认，也说明「迟到回复被当成重试成功」这个形状值得单独立记录。**
2. **`AgentChannelTransport.request` 仍然不关联回复 `id`**，这是它文档化的契约（它只负责「一行进、一行出」）；关联只存在于 `GuestClient._validate_reply`。所以任何直接用 transport 的调用方都可能被交给一个属于别的交换的回复。这不是缺陷而是边界：上面那个修复过的测试现在正是这条边界的守界测试。**客人侧实现者必须知道：协议层没有第二道防线。**

## Evidence-required

从 UNVERIFIED 变成已验证，需要留存的证据（M10 / M10a / M11 / M12 各取所需）：

| 证据 | 说明 |
| --- | --- |
| 通道状态记录 | `AgentChannelTransport.state` 的实测值。`silent` 是今天的实测结果，**不得**写成 `answered`；`connected` 本身不是证据 |
| 客人侧代理在场 | 客体进程表里出现该代理，且它打开 virtio-serial 端口（端口名 `unipad-agent-transport`）——这一步今天连「枚举到设备」都没达到 |
| 安装/注册证据 | 应用容器建立、注册记录、SpringBoard 真实图标（ADR-0003 证据表同款） |
| 信任证据 | 客体对签名/信任策略的**原始**接受或拒绝反馈；被拒时要留下拒绝码 |
| 启动证据 | 客体对 `launch.request` 的原始应答（含 `stage` 与 `error_code`），以及同一 run 的 M11 挑战值比对 |
| 持久化证据 | 客体对 `persist.request` 的原始应答（`markers_written` 逐条、`bytes_written`），加上**一次模拟重启后**新进程回读同一清单的结果 |
| 运行时导入证据 | 一个此前未安装的 IPA 在桌面启动后被导入；并记录它**没有**要求重编 XNU、没有重新生成整套系统镜像，以及**这次导入之外没有再动过镜像** |
| 迟到请求处置记录 | 代理就绪前收到过哪些请求、丢弃了几条（`GuestAgentStub.discarded` 是同形状的记录），用以证明它没有延迟应答 |
| 失败样本 | PRD §5.2 用例 10；以及一次启动失败、一次持久化失败各自的原始码 |
| 日志走查 | 所有留存的应答帧里无账号、令牌、签名私钥、用户文档路径（PRD §9.2） |
