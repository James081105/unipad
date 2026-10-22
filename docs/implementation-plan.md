# UniPad 代码阶段实施计划（2026-10-22）

本文件记录本轮代码阶段的推进结构与四字段约定（落实项目 CLAUDE.md「代码阶段与现场阶段分离」）。

## 推进轮次

| 轮次 | 内容 | 推送 |
| --- | --- | --- |
| R0 | git 基线：PRD 入 docs/、host-inventory.json（WP00 真实采集）、目录约定 | ✅ 已推送 |
| R1 | 脚手架与共享契约：errors.py / models.py / pyproject / config 模板 | ✅ 已推送 |
| R2 | Workflow#1：并行实现 controller / host / ipa / guest / scripts / workflows / docs（7 agent + 集成冒烟） | 进行中 |
| R3 | Workflow#2：fakes（假 QEMU / 假 QMP / 假 guest）+ 单元测试 + M0–M4 与 IPA-MVP 模拟验收套件 | 待 |
| R4 | 全量测试运行与修复（Windows 本机 pytest） | 待 |
| R5 | Workflow#3：五维审计（反向 / 性能 / 规范 / 调用链 / 对质变异测试）+ 对抗性验证 + 修复 | 待 |
| R6 | 终验、推送、审计验收报告（含现场未验证项清单） | 待 |

## 四字段记录（每个阶段必须分别记录，不得互相冒充）

| 阶段 | 实现状态 | Windows/模拟测试状态 | 现场验证状态 | 阻断原因 |
| --- | --- | --- | --- | --- |
| WP00 基线冻结 | CODE COMPLETE（R0/R1） | PASS（host-inventory.json 真实采集、doctor 可运行） | UNVERIFIED | 现场=用户本机；QEMU/IPSW 未到位 |
| WP01 干净构建 | UNVERIFIED（需 macOS runner + 冻结版本） | 模拟（CI preflight 可验证输入契约） | BLOCKED | sources.lock.json 未 frozen；Actions 预算未授权 |
| WP02 Windows UEFI | 代码完成（启动器/参数/QMP/日志） | 模拟（fake QEMU + 假 QMP 服务器） | BLOCKED | 本机无 QEMU 二进制、无固件产物 |
| WP03 引导交接 | 代码完成（证据采集/实验记录） | 模拟 | BLOCKED | 依赖 WP02 现场产物 |
| WP04 内核进入 | 代码完成（配置/故障分类） | 模拟（故障注入） | BLOCKED | 依赖 WP03 |
| M10–M12 IPA 链路 | 代码完成（预检/安装状态机/协议/fake guest） | 模拟（FakeGuestService 全流程 + 故障注入） | BLOCKED | 无真实客体安装服务 |
| CI 流水线 | CODE COMPLETE（4 个 workflow） | 模拟（结构与输入校验可本地检查；真跑需 GitHub） | UNVERIFIED | Actions 运行需推送后观察 |

## 判定规则

- CODE COMPLETE ≠ 现场 PASS；SIMULATION PASS ≠ 现场 PASS。
- 现场（用户 Windows 本机启动 iPadOS）未执行前一律 UNVERIFIED/BLOCKED，并在最终报告列明。
- 任何「成功」必须有证据：实验记录 + 产物哈希 + 运行日志 + 验证器结论；证据缺失即未验证。
