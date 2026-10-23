# ADR-0008: 自控 HTTPS 包服务的生产调用方属于「安装的传输阶段」，不属于 `start`

- 编号：ADR-0008
- 日期：2026-10-23
- 状态：accepted (code stage) —— 代码阶段已采纳；真机现场验证 **UNVERIFIED**
- 关联需求：PRD §3.2 P1 第 1 条（「通过 VirtIO Net 等路线联网，优先出站网络和自控 HTTPS 服务」）、§6 M13、FR06（运行时导入 IPA）
- 关联契约：`unipad.host.httpsvc.PackageServer` / `guest_fetch_url` / `hostfwd_spec`、`unipad.guest.protocol.InstallRequest`、`unipad.controller.cli._import_ipa`

## Context

`PackageServer`（`src/unipad/host/httpsvc.py:890`）是一套完整的自控 HTTPS 包服务：上下文管理器、TLS 默认开启、回环默认钉死（`allow_non_loopback=False` 时拒绝非回环绑定）、端口校验、自签证书指纹，以及 `guest_url`（`https://10.0.2.2:<port>/`，访客经 QEMU 用户态网关到达，不需要 `hostfwd`）。

它在测试里被完整地使用过——TLS 握手、路径穿越、生命周期都真跑过——**但它在生产代码里没有任何调用方**。于是 PRD §3.2 P1 第 1 条里「自控 HTTPS 服务」这一项目前是一段从未被启动过的代码：IPA 的字节走的是访客通道的消息路径（`guest/client.py:_record_delivery`、`guest/fake.py:_check_transfer`），不是网络路径。

## 我最初的想法是错的，记录为什么

我的第一反应是「让 controller 的 `start`/`stop` 持有 `PackageServer` 的生命周期」。读代码之后这个方案不成立，有两个独立的原因：

1. **`start` 不阻塞。** `cmd_start`（`cli.py:925-1060`）启动 QEMU 后立刻返回（`:1015` `launcher.launch(...)`，`:1023` 写 `launch.json`，`:1060` `return _ok(...)`）。在 `start` 里启动 `PackageServer` 会得到一个 Daemon 线程挂在一个马上退出的进程上——端口会在进程退出时释放，服务实际上从未对任何人生效过，而且释放时机不可控。

2. **`stop` 在结构上无法停止它不是自己启动的进程。** `cmd_stop`（`cli.py:1540-1595`）的 docstring 把这一点写得很清楚：优雅的 QMP `quit` 只能由启动它的那个进程驱动，第二次 CLI 调用只能证明两件事——记录的 pid 没了，或者 pid 还活着但这个 controller 不拥有它；后者报 `blocked` 而不是去杀一个主机包从未交给它的 pid。一个由 `start` 拉起来、跨进程存活的包服务会落入完全相同的困境，而且更糟：`stop` 连该报什么都不知道。

## Decision

**`PackageServer` 的生产调用方是 `_import_ipa` 的传输阶段（`cli.py:1087` 起），以上下文管理器的形式，生命周期恰好覆盖「字节必须搬过去」的那一个窗口。**

理由：

- 传输阶段是唯一一个**确实需要**字节到达访客的同步阶段，也是唯一一个会阻塞、因而能真正持有服务到使用完毕的进程。
- 窗口最小化：服务只在传输期间存在，端口必定被释放（`PackageServer.__exit__` 保证，body 抛异常也释放）。
- 与 QEMU 本身的生命周期完全解耦：QEMU 是长跑的，包服务是瞬时的，两者不该由同一个命令持有。

具体约束：

1. **只绑 `127.0.0.1`**，TLS 开启。这两项是 `PackageServer` 的默认值，本 ADR 不引入任何放开它们的路径。访客经 `10.0.2.2` 到达，不需要 `hostfwd`，`-nic` 除了设备本身不需要任何改动。
2. **根目录是 IPA 所在目录**，不是整个 run 目录——服务暴露的表面越小越好。
3. **端口已占用是拒绝，不是退让。** 换一个端口静默重试会让运行证据里的 URL 与实际监听的东西不一致。
4. **`guest_url`、证书指纹、端口、IPA 的 sha256 全部写入运行证据**（`install.json` 与 `launch.json`）。P1(1) 的验收要能回答「这次安装字节是从哪个源、用什么证书、经哪条路到的访客」，证据缺失即未验证。
5. **协议增补是可选的，不是必填的。** `InstallRequest` 增加可选的 `package_url` 与 `cert_fingerprint`。两者缺席时行为与今天完全一致——fake 访客和消息路径不受任何影响，既有测试不改。

## 传输方式是一个显式选择，且默认值必须诚实

传输阶段获得一个 `transport` 选择：`channel`（今天的默认，fake 使用的路径）或 `https`（`PackageServer` 源）。

**默认值保持 `channel`。** 理由：这台主机上还不存在真实访客，`https` 传输路径没有任何客体侧实现可以对接。把默认改成 `https` 会让每一次安装都在走一条从未被验证过的路，而失败会以「安装失败」的形式出现，把「传输方式选错」伪装成「包有问题」。

相应地，验收门禁必须报告每次安装**实际使用**的传输方式，并且不允许在没有真实访客的情况下声称 `https` 传输已被验证。

## 后果

- P1(1) 的「自控 HTTPS 服务」从「一段有测试但没有调用方的代码」变成「安装路径上一个可选的、有证据记录的传输方式」。这是代码阶段能走到的极限；**它是否真的能工作，取决于真实访客是否存在**。
- `PackageServer` 的测试已经有 TLS 握手、路径穿越和生命周期覆盖；新增的是「生产调用方真的会启动它、记录它、并一定释放它」的回归测试。
- 没有引入任何放开回环绑定、关闭 TLS 或使用 `hostfwd` 的路径。`hostfwd_spec` 保持它 docstring 里说的用途——主机侧工具进入访客，与包服务无关。

## 四字段

| 字段 | 状态 |
| --- | --- |
| 实现状态 | **CODE COMPLETE（代码阶段）**。`_import_ipa` 的传输阶段持有 `_TransferWindow`（`cli.py`），`--transport channel\|https` 挂在 `install` 与 `update` 上，默认 `channel`；`https` 时以 `PackageServer` 发布 IPA 自身目录，端口占用即拒绝，窗口结束必定释放端口 |
| Windows/模拟测试状态 | **SIMULATION PASS**。`tests/unit/test_cli_lifecycle.py` 与 `tests/unit/test_host_httpsvc.py` 覆盖：服务真的启动并记录 URL 与指纹、body 抛异常也释放端口、端口已绑定被拒绝且不重试、缺席 `package_url` 时请求与旧五字段逐字节相同、默认传输仍是 `channel`、证据记录实际使用的传输方式、明文/非回环仍被拒绝 |
| 现场验证状态 | UNVERIFIED —— 无真实访客，`https` 传输不可达；`guest_fetch_verified` 恒为 `False` |
| 阻断原因 | 缺真实访客安装服务；`transport=https` 在没有客体侧实现前不得成为默认。另记一笔与本 ADR 无关的既有事实：默认的 `channel` 路线在本机也走不通——`_CAPABILITIES["guest_channel"]` 解析到 `GuestClient`，而 `GuestClient.__init__(transport, ...)` 的第一个位置参数没有调用方给得上（`_call_capability` 按零参工厂调用它），两条路线都以 `guest_channel_unavailable` 拒绝。该 TypeError 在 `HEAD` 上一字不差地存在，不是本 ADR 引入的；修它需要一个新的访客通道工厂（`unipad/guest/__init__.py` 不在本 ADR 文件范围内），因此只记录不修改 |

## 实现记录（代码阶段落锤后补记）

落地形状，供后续审查对照：

- 传输阶段新增 `_TransferWindow`（上下文管理器）与 `_PackageSourceChannel`（只往 `request_install`
  的元数据里加 `package_url` / `cert_fingerprint`，不新增字节路径——本机字节仍走通道）。
  窗口只包住 `installer.install(...)`，也就是字节真正移动的那一段。
- 证据由 `unipad.host.httpsvc.service_evidence` 生成，URL/端口从**运行中的服务**回读，
  因此 `port=0` 也不会把「请求的端口」和「内核给的端口」混为一谈。
- 对第 4 条的一处收敛：ADR 原文写证据同时落 `install.json` 与 `launch.json`。安装是独立的一次
  CLI 调用，`launch.json` 由 `start` 写、且此时未必存在，`_import_ipa` 结构上碰不到它；
  因此证据落在 `install.json` 与命令信封里，`launch.json` 不写、也不假写。
- 网关侧：PRD 3.2 P1 (1) 的验收门禁（`src/unipad/acceptance/p1.py`）以「`src/` 下是否存在
  构造并启动 `PackageServer` 的位置」判断有无生产调用方。本 ADR 落地后该判据为真，
  门禁结论应从 `no_production_caller` 转为 `guest_outbound_requires_a_real_ipados_guest`；
  门禁自身及其测试不在本 ADR 的文件范围内，需由其owner同步。

