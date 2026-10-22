# ADR-0004: 图形路线（Graphics Route）

- 编号：ADR-0004
- 日期：2026-10-22
- 状态：accepted (code stage) —— 软件 ramfb/GOP 优先路线已采纳；VirtIO GPU 探测与 Vulkan 桥接 **UNVERIFIED / 推迟**
- 关联需求：PRD §2.1 E02/E07、§2.2-2、§2.3 H02、§3.3 及「优先级例外」、§4.2「固件显示/GPU」行、§4.3、§6 M9/M9a/M14/M15
- 关联契约：`config/machine.json` `display`（`format: ramfb`）、`unipad.errors.ErrorCode.RENDER_FAILED`、PRD §9.2 GPU 开启时的宿主桥接日志

## Context

1. 上游作者自述在 Mac 上有 GPU Metal 桥接，并报告软件图形路径存在**设置白屏**、Safari 还缺 AppOS（PRD §2.1 E02/E03，作者自述、未独立复现）。既不能沿用「完全没有输入和 Metal」的旧结论，也不能视为 Windows 已支持。
2. PRD §2.2-2 固定判断：上游 Mac Metal 方案依赖宿主 Metal；迁往 Windows Vulkan 涉及 API、资源、同步、着色器与显示适配，**RTX 3060 只是可用硬件条件，不代表这些软件已存在**。
3. PRD §2.1 E07 固定判断：QEMU VirtIO GPU 提供多种显示/加速后端，各自有宿主与客体要求；**不能把加入 VirtIO GPU 参数当成 iPadOS 已获得 Metal**。
4. PRD §2.3 H02 假设「软件图形路径足够支持首个 UIKit App」，最小验证是「显示真实 App 窗口、按钮和动态文字」。
5. PRD §3.3 有明确例外：若首个 UIKit App 因缺少必要图形能力而无法显示，**最小可用图形路径属于 P0 阻塞**；完整 Metal 支持与 GPU 性能优化仍属后续范围。
6. 宿主侧存在多个虚拟显示适配器（`host-inventory.json` `gpu.virtual_displays`），窗口枚举可能受干扰；宿主截屏/取帧必须按 QEMU 窗口标题/PID 定位，不能按适配器。

## Decision

按顺序分四步，每步有独立证据门槛；**前一步未取证不得进入下一步**。

1. **第一步（M1–M9）：软件 ramfb/GOP 优先。**
   `config/machine.json` 的 `display.format` 固定为 `ramfb`；不添加 VirtIO GPU 参数、不添加任何宿主 GPU 后端。目标只是「固件显示路径可用」：像素格式、stride、分辨率与客体映射一致（PRD §4.2）。
   *证据*：固件日志 + 实际客体显示（非静态图片）+ 带时间戳截图；QEMU 实际参数记录。只跑出 QEMU 窗口不算通过（PRD §6 M1）。
2. **第二步（M9a）：最小 UIKit 渲染路径，P0 阻塞判定点。**
   用首个自有 UIKit 测试 App 验证首帧、更新与输入反馈。若 App 因缺少必要图形能力无法显示，最小图形兼容工作**提升为 P0**（PRD §3.3 例外）；仍无法实现则标记阻塞，不得用桌面壁纸截图代替（PRD §6 M9a）。
   *证据*：测试 App 首帧、动态文字更新、点击后帧变化的带时间戳截图；宿主发送事件与对应帧改变的时间戳对。
3. **第三步（M9a 起探测，M14 验收）：VirtIO GPU 软件路径探测。**
   在第二步成立后，才允许以**实验参数**形式探测客体 VirtIO GPU 前端与 QEMU 软件后端（`-device virtio-gpu` + 软件渲染后端）。这是「能否减轻 ramfb 路径限制」的实验，不是 Metal 支持；后端、协议、着色器、同步、呈现需逐项成立。
   *证据*：客体前端探测证据、协议交互日志、与 ramfb 路径的对照截图差异、失败时的明确错误分类（`RENDER_FAILED`）。只枚举出设备不算通过（PRD §6 M14）。
4. **第四步（P2，推迟）：Windows Vulkan 桥接与 RTX 3060 加速。**
   范围至少包括资源生命周期、缓冲区/纹理、着色器输入到宿主可执行形式、命令提交、同步、IOSurface/显示互通与错误处理（PRD §4.3）。**在第一步/第二步成立前不投入实现**；「Metal → Vulkan」一行文字不构成方案。
   *证据*：最小渲染样例的客体命令到 3060 的真实执行、图像正确性与性能数据；GPU 开启时另存宿主桥接日志（PRD §9.2）。只见 RTX 使用率上升不算通过（PRD §6 M14）。

## Consequences

**正面**

- M1–M9a 不依赖任何宿主 GPU 工作，可在 Windows 代码阶段用 fake 显示/截图链路完成参数组装与证据采集逻辑。
- 「最小图形路径是 P0 阻塞」被写成明确判定点，避免图形问题被无限期推成 P1/P2。
- 三步之间有硬门槛，杜绝「加了 VirtIO GPU 参数」被宣称为 Metal 加速。

**负面 / 代价**

- 若第二步失败，MVP 时间线被最小图形兼容工作阻塞（这是 PRD 明示的代价，不是计划失误）。
- 软件路径性能可能不足以支撑交互验收；PRD §5.3 的 UI 帧率等指标在硬件盘点前不设保证值。
- VirtIO GPU 探测会引入新的失败面（协议不同步、后端不可用、客体驱动缺失），必须与 ramfb 路径可对照、可回退。

**不做的事**

- 不把窗口显示使用了宿主 GPU 当作客体 Metal 加速（PRD §4.3）。
- 不在没有最小渲染样例前承诺 Windows Vulkan 桥接或 M15 Metal IPA。
- 不用静态主屏幕截图代替 App 首帧与交互反馈。

## Status

accepted (code stage)（第一步/第二步的路线与 P0 例外规则）；第三步探测与第四步 Vulkan 桥接为 **proposed / 推迟至 P2**。所有图形现场验证（M9a、M14、M15）**UNVERIFIED**；宿主侧待代码阶段完成的是 ramfb 参数组装、显示尺寸/格式断言、截图与帧对证据采集，以及 VirtIO GPU 实验参数的显式开关（默认关闭）。

## Evidence-required

| 步骤 | 必备证据 | 不满足时的处置 |
| --- | --- | --- |
| M1–M9 ramfb | 固件日志、客体实际显示、带时间戳截图、QEMU 参数 | 分离 QEMU 配置与客体问题；标记具体 blocked 项 |
| M9a 最小 UIKit | 测试 App 首帧/更新/输入反馈截图与时间戳对 | 提升为 P0 最小图形兼容；仍失败则标记阻塞 |
| M9a/M14 VirtIO GPU | 客体前端探测、协议日志、ramfb 对照差异 | 保留 ramfb 路径；记录后端不可用原因 |
| M14/M15 GPU 加速 | 客体命令到 3060 的真实执行、图像正确性、性能数据、宿主桥接日志 | 停留在软件路径，不外推兼容性 |
