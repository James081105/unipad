# UniPad

**Run real iPadOS userland on an Intel Windows host via QEMU AArch64 TCG + UniXNU, then install and interact with DRM-free ARM64 Device IPAs.**

> 状态：代码阶段进行中。真机验收（在用户 Windows 本机启动 iPadOS）尚未执行，标记为 UNVERIFIED。
> PRD：[`docs/PRD.md`](docs/PRD.md)（UniPad PRD v0.1，2026-10-22）

## 当前基线

| 项 | 值 |
| --- | --- |
| 目标宿主 | Windows 11 Home, Intel i7-11800H 8C/16T, 16GB RAM, RTX 3060 Laptop |
| 加速器 | QEMU **TCG（显式）**；Hyper-V 已启用，WHXP 需在 M0 探测 |
| 客体候选 | iPad7,1 / iPadOS 17.7.11 / build 21H161（以 `config/sources.lock.json` 固定提交为准） |
| 宿主盘点 | [`host-inventory.json`](host-inventory.json)（WP00，真实采集） |

## 仓库结构（实施设计见 PRD §9）

```
config/       sources.lock.json, machine.json, budget.json（拟建，模板见 config/README.md）
src/unipad/   controller（状态机/实验/预算/验证器/CLI）+ host（QEMU/QMP/镜像/日志/输入）+ ipa（预检/安装）+ guest（协议/客户端/假服务）
scripts/windows/  PowerShell 入口：盘点、下载校验、启动、诊断
scripts/macos/    macOS 构建适配入口（仅 macOS 执行，Windows 上为文档化存根）
tests/        单元测试 + fakes（假 QEMU/假 QMP/假安装服务）+ acceptance（M0–M4 与 IPA-MVP 模拟门禁）
.github/workflows/  preflight / build-macos / verify-artifacts / publish
docs/decisions/    版本、持久 Data、安装通道、图形路线决策记录
runs/<experiment-id>/  运行证据（不入 Git）
artifacts/      本地构建产物与已知可启动版本指针（大二进制不入 Git）
local-private/  IPSW、客体镜像、种子、密钥（**绝不入 Git**）
```

## 开发约定

- Python 3.12 + pytest；包管理见 `pyproject.toml`。
- 每次实验记录遵循 PRD §7.3 `experiment.json` 格式；证据最小集合见 §9.2。
- 禁止事项（PRD 附录 A）：不得把缺失证据补写为成功、不得用旧包冒充当前构建、不得把 mockfs PID 1 当系统 launchd、不得把预装图标或静态画面当 IPA 运行。
- Git commit message 使用英语（见 `CLAUDE.md`）。

## 快速开始（代码阶段，Windows）

```powershell
python -m pip install -e .          # 安装 unipad 包
python -m pytest tests -q           # 运行全部单元与模拟验收测试
python -m unipad doctor             # 宿主与工具链盘点（WP00 复查）
```
