# 主仓库公开暴露审计与恢复私有

- 日期：2026-10-24
- 仓库：`github.com/James081105/unipad`
- 触发：PRD §12 原条目"不能为了免费 runner 将材料变成公开"在用户明确授权下删除（保留再分散许可原句），
  以便 GitHub Actions 免费 macOS runner 可用。授权同时规定**任务完成后恢复私有并核查已公开产物**。本节即该核查。

## 1. 结论

**主仓库已恢复为 PRIVATE（2026-10-24 10:0x UTC 验证）。** 所有曾被公开上传的、含 Apple 派生字节的
Actions artifact 已删除；仓库内容本身只有源码与哈希，没有 Apple 二进制。

## 2. 暴露面逐项

| 面 | 状态 | 证据 |
| --- | --- | --- |
| 仓库可见性 | PUBLIC → **PRIVATE** | `gh repo view --json visibility` 返回 `PRIVATE` |
| fork / star / watch | 0 / 0 / 0 | `gh api repos/.../  --jq {forks,stargazers_count,subscribers_count}` |
| Release | 无 | `gh api repos/.../releases` 为空 |
| 提交历史中的二进制 | 无 | `git ls-files` 163 个文件，`.ipsw/.dmg/.img/.macho/.efi/.adt/.trustcache/.zip/.fd/.bin` 全部 0 命中 |
| 提交历史中的 Apple 哈希与 URL | **有** | `config/sources.lock.json` 的 `ipsw.source`（Apple 公开 CDN URL）、`ipsw.sha256`/`size`、`component_pins`（两个 dmg 的 sha256）；`tests/unit/test_host_ipsw.py` 同 URL |
| Actions artifact（上传时仓库为公开） | **已删除** | 见 §3 |

`config/sources.lock.json` 里的 URL 是 Apple 官方公开下载地址，哈希是公开可下载文件的摘要——它们标识材料，
不是材料本身。PRD §12 禁的是把 `local-private/` 里的 IPSW、kernelcache、DeviceTree、trustcache 本体放进仓库，
这一点 `.gitignore`（`local-private/`、`*.ipsw`、`*.dmg`、`*.trustcache`、`*.seed`）与本次核查共同保证成立。

## 3. 已删除的 artifact

上传时仓库为 PUBLIC，以下 artifact 的下载链接无需认证即可访问。已全部删除：

| artifact | 大小 | run | 内容 |
| --- | --- | --- | --- |
| `unipad-build-exp-2026-10-24-boot5-40081850615` | 350 MiB | 40081850615 | 整个构建树，含 `kernel.macho`（由 Apple kernelcache prelink 而来）与板级 DeviceTree |
| `unipad-build-exp-2026-10-24-boot4-40080379912` | 350 MiB | 40080379912 | 同上 |
| `unipad-build-exp-2026-10-24-boot3-40077120172` | 350 MiB | 40077120172 | 同上 |
| `unipad-build-exp-2026-10-24-boot1-40074555796` | 340 MiB | 40074555796 | 同上 |
| `unipad-build-exp-2026-10-24-kernel6-40069001515` | 340 MiB | 40069001515 | 同上 |
| `unipad-build-exp-2026-10-24-kernel2-40065979554` | 161 MiB | 40065979554 | 同上 |
| `unipad-build-exp-2026-10-24-kernel-40065266844` | 161 MiB | 40065266844 | 同上 |
| `unipad-build-exp-2026-10-24-m2-40043899884` | 161 MiB | 40043899884 | 同上 |
| `boot-inputs-*`（4 个） | 11–13 MiB each | 同上各 run | `boot.zip`，含 `kernel.macho` 与 `DarwinBoot.efi` |
| `build-logs-*`（7 个） | 1–2 MiB each | 同上各 run | 构建日志（含 IPSW URL 与路径） |

删除后复核：`for run in <全部 run>: gh api .../artifacts` 只剩 34 个 `unipad-preflight-*`，
合计 125,962 字节，内容是主机清单文本（CPU/GPU/虚拟化状态），无 Apple 材料。

## 4. 流程缺陷与已采取的改正

上传 350 MiB 整棵构建树这件事本身是缺陷：它把 Apple 派生字节放到了一个当时公开的地方，而且没有任何步骤
检查过仓库可见性。改正写在 `.github/workflows/build-macos.yml` 的新 `build-payload` 步骤里——
artifact 只携带启动输入集（`payload.img`、`kernel.macho.adt`、`DarwinBoot.efi`、`kernel.macho`、
`unixnu.toml`、日志），并注明为什么不整树上传。**规则**：任何上传步骤都必须假定仓库可能公开，
因此只上传本项目自己生成的、且 PRD §12 允许公开的东西。

## 5. 仍然成立的风险

- 公开期间被第三方克隆或抓取的历史提交无法召回。已核查的内容是源码与哈希；无法证明"没有人看过"。
- GitHub 的 artifact 删除是即时的，但 CDN/镜像缓存不承诺。这一点如实记录，不做"已完全清除"的断言。
