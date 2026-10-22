# UniPad 代码阶段实施计划（2026-10-23 更新）

本文件记录本轮代码阶段的推进结构与四字段约定（落实项目 CLAUDE.md「代码阶段与现场阶段分离」）。

## 推进轮次

| 轮次 | 内容 | 推送 |
| --- | --- | --- |
| R0 | git 基线：PRD 入 docs/、host-inventory.json（WP00 真实采集）、目录约定 | ✅ 已推送 |
| R1 | 脚手架与共享契约：errors.py / models.py / pyproject / config 模板 | ✅ 已推送 |
| R2 | Workflow#1：并行实现 controller / host / ipa / guest / scripts / workflows / docs | ✅ 已推送（7cf342b / 97014b0） |
| R3 | Workflow#2：fakes + 单元测试 + M0–M4 与 IPA-MVP 模拟验收套件（含 6 处缺陷修复） | ✅ 已推送（a391498） |
| R3b | 修好文档化但失效的 `python -m unipad` 入口（补 `unipad/__main__.py` + 回归测试） | ✅ 已推送（5d67b06） |
| R4 | 全量测试运行与修复（Windows 本机 pytest，520 项） | ✅ 完成 |
| R5 | Workflow#3 五维审计：反向 / 性能 / 规范 / 调用链 / 对质（变异测试） | ✅ 变异轮完成（15/15 变异均被发现，无测试空洞） |
| R5b | 修复审计确认的 CRITICAL 发现 RR-01（安装门禁把 failed 当成功）+ ADR-0007 + 版本升版 | ✅ 已推送（ed4414d） |
| R5c | M0 源身份固定：sources.lock.json 填实提交/IPSW 哈希，解决 21H161/21H461 build 号分歧 | ✅ 已推送（f0c106a） |
| R6 | Workflow#4：Windows 真实启动关键路径（工具链 / IPSW 抽取 / LZFSE 解码器 / XNU 可移植性 / QEMU argv / P1 设计） | 进行中 |
| R7 | P1 代码实现（Windows 可编码部分）+ 全量测试 + 审计 | 待 |
| R8 | 终验、推送、审计验收报告（含现场未验证项清单） | 待 |

## 四字段记录（每个阶段必须分别记录，不得互相冒充）

| 阶段 | 实现状态 | Windows/模拟测试状态 | 现场验证状态 | 阻断原因 |
| --- | --- | --- | --- | --- |
| WP00 基线冻结 | CODE COMPLETE（R0/R1/R5c：源身份已按证据固定） | PASS（host-inventory.json 真实采集、doctor 可运行、锁文件提交为 40 位 SHA） | UNVERIFIED | 现场=用户本机；`frozen` 仍为 false（QEMU/clang 版本未记录、未跑过干净构建） |
| WP01 干净构建 | UNVERIFIED（需 macOS runner + 冻结版本） | 模拟（CI preflight 可验证输入契约） | BLOCKED | `sources.lock.json` 未 frozen；Actions 预算未授权；本机无 Xcode |
| WP02 Windows UEFI | 代码完成（启动器/参数/QMP/日志） | 模拟（fake QEMU + 假 QMP 服务器） | BLOCKED | 本机 QEMU 安装包已下载未安装；固件产物未就绪 |
| WP03 引导交接 | 代码完成（证据采集/实验记录） | 模拟 | BLOCKED | 依赖 WP02 现场产物 |
| WP04 内核进入 | 代码完成（配置/故障分类） | 模拟（故障注入） | BLOCKED | 依赖 WP03；XNU 补丁内核构建链路待证 |
| M10–M12 IPA 链路 | 代码完成（预检/安装状态机/协议/fake guest） | 模拟（FakeGuestService 全流程 + 故障注入） | BLOCKED | 无真实客体安装服务 |
| 验证器逻辑 | CODE COMPLETE（R5b：install.stage_terminal 改判成功终态，VERIFIER_VERSION 1.1.0） | PASS（新测试先在旧实现上失败、再经变异复现，均已恢复） | UNVERIFIED | 真机安装门禁表现需现场各跑一次成功/失败 |
| CI 流水线 | CODE COMPLETE（4 个 workflow） | 模拟（结构与输入校验可本地检查；真跑需 GitHub） | UNVERIFIED | Actions 运行需推送后观察 |

## 判定规则

- CODE COMPLETE ≠ 现场 PASS；SIMULATION PASS ≠ 现场 PASS。
- 现场（用户 Windows 本机启动 iPadOS）未执行前一律 UNVERIFIED/BLOCKED，并在最终报告列明。
- 任何「成功」必须有证据：实验记录 + 产物哈希 + 运行日志 + 验证器结论；证据缺失即未验证。

## 本轮新确立的事实（均有证据，未经现场验证不得外推）

1. **IPSW 已到位且哈希吻合**：`iPad_Pro_HFR_17.7.11_21H461_Restore.ipsw`，6,835,124,840 字节，sha256 `c84ad24b…45d1`；board.toml 钉住的两张 dmg 哈希与实测一致。
2. **build 号分歧已解决为 21H461**（三份独立证据，见 `config/sources.lock.json` 的 `system_identity.resolution`）。
3. **XNU 内核构建不一定需要 xcodebuild**：`scripts/build-xnu-4k-baseline.sh` 全文没有调用 xcodebuild，实际是 `make -C .cache/xnu-work build SDKROOT=iphoneos …` + clang；`kernel.py:75` 的 `tool('xcodebuild')` 只是存在性检查。脚本里的 macOS 专有项是 `ditto`（3 处）与 `build-firehose.sh`（xcodebuild + xcrun lipo），而后者只在 `libfirehose_kernel.a` 不存在时才运行（第 122 行）。**该结论正在 Workflow#4 的 xnu-windows-port 探针下做严格证明，尚未定论。**
4. **本机无任何构建工具链**：clang / llvm-nm / llvm-objcopy / make / cmake / ninja / unifdef / mformat / mcopy 全部缺失（仅有 gzip 与 Python 3.12.9）；C: 仅剩约 3.7 GB，一切必须装到 F:。
5. **完整 iPadOS 启动（`--os-root`）在 Windows 上结构性 BLOCKED**：上游 `run-qemu.py` 调用 hdiutil 并把 `/dev/rdisk` 作为 QEMU host_device 传给 QEMU，Windows 无等价物。
