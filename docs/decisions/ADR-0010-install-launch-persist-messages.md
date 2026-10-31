# ADR-0010: `launch` 与 `persist` 上线的独立决策记录（ADR-0003 推测协议的修订）

- 编号：ADR-0010
- 日期：2026-10-31
- 状态：proposed (code stage) —— 代码阶段已采纳；**客体侧实现 UNVERIFIED / BLOCKED（本机没有任何客体安装服务可以回答这两条消息）**
- 关联需求：PRD §3.1 FR06（运行时导入：检查、传输、安装、注册、状态反馈）、§5.2 用例 3–4/10、§6 M9b/M10/M10a/M11/M12、§9.1 安装接口契约、§9.2（无账号/令牌/用户文档）、§10（故障分类封闭集）
- 关联契约：`unipad.guest.protocol`（新增 `MSG_LAUNCH_REQUEST` / `MSG_LAUNCH_RESPONSE` / `MSG_PERSIST_REQUEST` / `MSG_PERSIST_RESPONSE`、`LaunchRequest` / `LaunchResponse` / `PersistRequest` / `PersistResponse`、`LAUNCH_STAGES` / `PERSIST_STAGES`）、`unipad.guest.client.GuestClient.launch` / `.persist`、`unipad.guest.fake.FakeGuestService.handle`、`unipad.errors.ErrorCode.{REGISTRATION_FAILED, MISSING_DEPENDENCY, DYLD_FAILED, PERSISTENCE_FAILED, INSTALL_SERVICE_UNAVAILABLE}`

> **编号说明（给整合者）：** 任务描述里写的是 `ADR-0008-install-launch-persist-messages.md`。**ADR-0008 已被占用**——`docs/decisions/ADR-0008-package-server-caller.md`（自控 HTTPS 包服务的生产调用方）自 2026-10-23 起就是 accepted，且 `src/unipad/guest/protocol.py` 与 `src/unipad/guest/__init__.py` 的注释、`src/unipad/acceptance/p1.py:1019` 的检查、`tests/acceptance/test_p0p1_gate_p1.py` 的四处引用都指向它。覆盖它会让一份 accepted 决策连同它的引用一起消失，所以本记录取 **ADR-0010**（下一个空号）。ADR-0003 对修订程序的要求（独立决策记录、不随功能修改偷偷放宽）由本文件满足，与编号无关。

## Context

1. **ADR-0003 把这份协议定义为推测接口，并预先写明了修订程序。** 它的「负面 / 代价」第一条原文：协议是**推测的接口**（PRD §2.3 H06「不先假定 API 格式兼容」），「若客体侧真实接口与协议假设不符，协议需要修订，修订必须作为独立决策记录，**不得随功能修改偷偷放宽**」。本文件就是那份记录：它新增了四个消息类型，是一次**扩词汇表**，不是一次放宽。
2. **`FakeGuestService` 早就建模了启动与持久化，但线上协议没有它们的消息类型。** `launch(bundle_id)`、`uninstall(bundle_id)`、`set_data_marker()`、`data_markers()`、`persist(dir)` / `load(dir)` 在 `src/unipad/guest/fake.py` 里都存在且被 `tests/unit/test_guest_fake.py` 钉住；`KNOWN_TYPES` 里只有 install / challenge / status 三对加 `error`。于是宿主想问客体「启动这个 App」或「把 Data 卷刷下去并报告」，**在协议层无话可说**——闭环的「启动」和「持久化」两环是断的。
3. **通道实验已经有一个真实传输层，改协议的代价因此变高了。** `unipad/guest/agent_channel.py` 是第一个对端是 QEMU 设备的传输层，它自己记录了六态（`unconnected|waiting|refused|silent|answered|closed`）和「一个打开的 socket 不是一个会回答的客体」。如果在通道实验跑到一半时才发现协议缺消息类型，那么「协议缺类型」和「客体不应答」会叠成同一个观测，实验要重做一遍才能分离。**在实验开始前把四类消息全部放上线，是让那次实验只测一件事。**
4. **闭环的四环缺一环，宿主状态机就不完整。** 用户要的闭环是 安装（install）→ 注册（register，随 install 的 `stage='registered'` 上报）→ 启动（launch）→ 持久化（persist）。只有 install/challenge/status 时，宿主无法在**不臆造**的情况下说出「这个 App 装好了但起不来」或「数据刷下去了」。把两者说成 install 的一种结果，正是 ADR-0007 刚刚拆开的那类错误（把「终态」当「成功的终态」）。
5. **存在一条被 ADR-0003 明确判死的捷径，且它在新消息下更容易走。** 把 `.app` 预置进 Data seed / 镜像，图标会出现在桌面上，`persist` 之后「数据也在」。PRD §10 末句与 §3.1 FR06 都明确**只修改 Data seed 的预装方案不通过 FR06**；ADR-0003 决定 4 只允许把它当 M10/M10a 的对照与诊断手段。新增 `launch` / `persist` 之后，一个预置的 `.app` 可以「被启动」且「有持久数据」，**看起来四环全通了**——所以本 ADR 必须把这条捷径再钉一次，而且要比 ADR-0003 更具体。

## Decision

1. **新增两对消息，拼写与既有完全一致（点分 `<动词>.request` / `<动词>.response`）：**

   | 常量 | 值 | 请求负载 | 应答负载 |
   | --- | --- | --- | --- |
   | `MSG_LAUNCH_REQUEST` / `MSG_LAUNCH_RESPONSE` | `launch.request` / `launch.response` | `LaunchRequest(bundle_id)`（必填） | `LaunchResponse(stage, bundle_id, trust_mode, error_code, error_message)` |
   | `MSG_PERSIST_REQUEST` / `MSG_PERSIST_RESPONSE` | `persist.request` / `persist.response` | `PersistRequest(path)`（必填） | `PersistResponse(stage, markers_written, bytes_written, error_code, error_message)` |

   拼写不是审美选择：`GuestClient._validate_reply` 用常量比对应答的 `type`，`decode()` 用 `KNOWN_TYPES` 判断是否 `unsupported_command`。下划线拼写（`launch_request`）根本不在线上，一个拼错的对端会被**行为正确**的客户端拒绝——`tests/unit/test_guest_agent_channel.py` 的 docstring 记录了 `status_response` 这个先例，`tests/unit/test_guest_protocol.py::test_launch_and_persist_are_the_dotted_spelling_and_nothing_else` 钉住两种拼写的差。

2. **`launch` 是独立的一问，不是 install 的一种结果。** 一个注册成功的 App 仍可能起不来（缺依赖、入口未执行）。`LaunchResponse.stage` 取 `LAUNCH_STAGES = ("launched", "failed")`；失败时 `error_code` 是客体**逐字**给出的分类码（未安装 → `registration_failed`，缺依赖 → `missing_dependency`，入口未执行 → `dyld_failed`）。客户端把它**返回**而不抛出，与 `install_package` 完全一致，以便调用方把它当证据记录。故障注入沿用既有 `dependency` / `dyld` 点（`INSTALL_FAILURE_POINTS` 是 `tests/unit/test_guest_fake.py::test_failure_points_are_the_documented_chain_order` 逐项钉死的，不新增点），所以**装了就是装了，只有启动被拒绝**——`tests/unit/test_guest_client.py::test_a_launch_failure_is_not_an_install_failure` 钉住这个可分性。

3. **`persist` 的两个数字都是客体的观测，不是宿主的推算。** `markers_written` 是刷写**之后**从 `data_markers()` 读回的清单（与 `StatusResponse.data_markers` 同一份事实），`bytes_written` 是实际落盘文件的 `stat().st_size`。宿主不得自己算任何一个：自己算等于报告一次它没看见的刷写。`persist` 的失败**不是**「没什么可持久化」——失败应答的 `markers_written` 为空是因为客体没有报告任何成功写入，不是宿主判定那里没有数据。

4. **`persist` 请求必须指名卷。** `PersistRequest.path` 必填。「刷默认的那个」是双方都无法验证的请求；创建并挂上 Data 卷的是宿主，所以知道名字的是宿主。这个字段在**真实客体**上的语义（挂载点？卷标？服务端点？）**未验证**；`FakeGuestService` 把它解释为模拟 Data 卷的目录，这是本机唯一能跑的解释。

5. **`launch` / `persist` 不放宽任何既有保证，只继承它们。** 具体承重的四条，全部由 `tests/unit/test_guest_client.py` 钉住：
   - 客体不应答 → `install_service_unavailable`，**绝不**返回一个默认的 `launched` / `persisted`；
   - 应答 `type` 或 `id` 不匹配 → `qmp_protocol_error`；
   - 声称成功却缺 `bundle_id`（launch）或同时带 `error_code`（两者）→ `qmp_protocol_error`；
   - 客体错误信封 → 失败的应答，码逐字通过。

6. **明确禁止的捷径（ADR-0003 决定 4 的具体化）。** 把 `.app` 预置进 Data seed / 镜像，**不因本 ADR 新增了 `launch` 与 `persist` 而变成达标**。判别方法只有一条，且不依赖新消息：**运行时导入一个此前未安装的 IPA**，且该导入**不要求重编 XNU、不重新生成整套系统镜像**。一个预置 App 能被 `launch`、`persist` 之后有数据，只证明「预置的副本会跑」，不证明「运行时导入链走得通」。**任何以预装图标 + 一次成功启动宣称 FR06 达标的记录都无效**（PRD §3.1、§10、附录 A 禁止事项）。

7. **`unipad.guest` 的导出面随之扩大**（`__init__.py` 的 `__all__`），使四个消息的模型可以从包级导入。这是导出面变更，不是接口放宽：`create_install_channel()` 仍然在没有传输层时拒绝（`install_service_unavailable` / `no_guest_transport`），本 ADR 不给它一个默认客体。

## Consequences

**正面**

- 宿主状态机四环齐备，`unipad.guest` 可以在**没有客体**的前提下被完整地驱动与测试（ADR-0003「正面」第三条的延续）。
- 通道实验（`agent_channel`）在此期间不需要改协议：`launch.request` / `persist.request` 已经在 `KNOWN_TYPES` 里，实验测的是「客体在不在」，不是「协议够不够」。
- 启动失败与安装失败在证据里是两个事实，M11 的「App 真的能跑」因此可被单独判定，而不是从安装结果里推断。

**负面 / 代价**

- **词汇表变大了，而它仍然是推测的。** ADR-0003 的警告原样成立：真实客体安装服务的接口形状未知，这四条消息的字段名、`persist` 请求的 `path` 语义、`launch` 的应答字段，都有可能在现场被推翻。届时按 ADR-0003 的程序再立一份独立决策记录修订，**不得**在功能提交里顺手改。
- **`LaunchResponse` / `PersistResponse` 的「成功」仍然只是客体自称。** 客户端能拒绝自相矛盾和不可归属的成功，但不能独立验证「App 真的起来了」或「数据真的落盘了」——前者需要 M11 的挑战值，后者需要 M9b 的模拟重启后回读。
- `FakeGuestService.handle` 多了两个分支，`tests/unit/test_guest_fake.py`（不属本文件属主）对 `INSTALL_FAILURE_POINTS` 与 `handle` 的既有断言仍然成立——本次没有改故障点词表，也没有改 `launch()` 方法自身的返回形状（它内部仍说 `running`，由 handler 翻译成线上的 `launched`，且**只接受这一种映射**，其它一律 `qmp_protocol_error`）。

**不做的事**

- 不把复制文件、解压 `.app`、出现图标、预置 App 能启动，当作安装完成或 FR06 达标。
- 不在没有客体信任证据的情况下宣称「签名合规」。
- 不因通道实验困难就把 FR06 悄悄改成「预装即可」。
- 不新增 `INSTALL_FAILURE_POINTS` 的点；`launch` 的故障注入沿用 `dependency` / `dyld`。
- 不把 `persist` 的 `markers_written` / `bytes_written` 变成宿主侧的计算结果。
- 不宣称本机存在任何能回答 `launch.request` / `persist.request` 的客体。

## Status

proposed (code stage)。代码阶段交付：`unipad.guest.protocol` 的两对新消息与四个模型、`GuestClient.launch` / `.persist`、`FakeGuestService.handle` 的两个分支、包级导出、三个测试文件的新增用例（`tests/unit/test_guest_protocol.py` 86、`test_guest_client.py` 86、`test_guest_agent_channel.py` 38；`tests/unit/test_guest_fake.py` 88 项不变）。

变异检查（`local-private/tmp/mutate-ipa-loop.py`，9 个变异，全部被测试捕获）：launch 把失败报成成功、persist 报出不持有的 marker、超时变成成功、应答 id 不关联、应答 type 不关联、persist 报出未测量的字节数、消息拼写从点分变成下划线、launch 声称成功却缺 bundle_id 被放行、fake 把任意 stage 改名成 `launched`。**其中第 9 个第一轮存活**：内存服务只返回 `running`，happy-path 测试永远到不了那道检查，它是「看起来是检查的死代码」。修复方式是补一个驱动返回 `starting` 的客体的测试，而不是宣称该守卫已被覆盖。

**客体侧 UNVERIFIED / BLOCKED。** 本机没有客体安装服务：`NativeVirtioConsole` 不在本板构建里（`agent_channel.py` docstring 记录），内核在该传输上枚举不到设备，QEMU 仍然接受 TCP 连接。已测量的状态是 `silent`——**连上了，没有应答**。因此这四条消息在真实客体上的行为（字段是否够、`path` 语义是什么、失败时给什么码）全部未验证。

四环的诚实状态：

| 环节 | 消息 | 状态 | 缺的那一件事 |
| --- | --- | --- | --- |
| 安装 | `install.request` | CODE COMPLETE（宿主侧）；客体侧 UNVERIFIED | 真实客体安装服务应答一次 |
| 注册 | `install.response.stage='registered'` | CODE COMPLETE（宿主侧）；客体侧 UNVERIFIED | 真实客体确认容器/图标/注册记录 |
| 启动 | `launch.request` | CODE COMPLETE（宿主侧）；客体侧 UNVERIFIED / BLOCKED | 真实客体确认入口执行（并给出 M11 挑战值） |
| 持久化 | `persist.request` | CODE COMPLETE（宿主侧）；客体侧 UNVERIFIED / BLOCKED | 真实客体刷写 Data 卷并报告，且模拟重启后回读一致 |

## Evidence-required

从 UNVERIFIED 变成已验证，需要留存的证据（M10 / M10a / M11 / M12 各取所需）：

| 证据 | 说明 |
| --- | --- |
| 通道状态记录 | `AgentChannelTransport.state` 的实测值。`silent` 是今天的实测结果，**不得**写成 `answered`；`connected` 本身不是证据 |
| 启动证据 | 客体对 `launch.request` 的原始应答（含 `stage` 与 `error_code`），以及同一 run 的 M11 挑战值比对 |
| 持久化证据 | 客体对 `persist.request` 的原始应答（`markers_written` 逐条、`bytes_written`），加上**一次模拟重启后**新进程回读同一清单的结果 |
| 运行时导入证据 | 一个此前未安装的 IPA 在桌面启动后被导入；并记录它**没有**要求重编 XNU、没有重新生成整套系统镜像 |
| 输入包 SHA-256 | PRD §5.2 用例 3 要求记录；并先证明该包此前未安装（与预置副本区分） |
| 信任证据 | 所采用的签名/信任方式、客体侧接受或拒绝的原始反馈 |
| 失败样本 | PRD §5.2 用例 10：导入不支持的平台包，返回明确原因且系统保持可恢复；以及一次启动失败与一次持久化失败各自的原始码 |
| 日志走查 | 所有留存的应答帧里无账号、令牌、签名私钥、用户文档路径（PRD §9.2） |
