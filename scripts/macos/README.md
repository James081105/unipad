# scripts/macos/

macOS 专用入口（PRD §4.1、§8.1）。Xcode、Apple SDK 与磁盘工具只存在于 macOS，
因此这些脚本**只在 macOS runner 上执行**，Windows 本机绝不运行。

| 脚本 | 职责 | 硬性规则 |
| --- | --- | --- |
| `build-macos.sh` | 按冻结版本构建指定阶段，产出带逐文件 sha256 的 `artifact-manifest.json` | 输入必须 frozen；拒绝移动分支构建；拒绝 HVF/TCG 混淆；绝不上传完整系统镜像或私有数据（PRD 8.2 步骤 4） |
| `prepare-image.sh` | IPSW/RootFS 准备（校验 sha256、解包、生成本地清单） | 只写 `local-private/` 之外的调用方指定目录；绝不写入仓库；产物绝不提交 |

Windows 侧通过 GitHub run ID 下载对应产物并核验后运行（PRD 8.2 步骤 5–6）。

四个 Apple 工具阶段（kernel / bootloader / devicetree / assemble-image）此前是
`TODO(macos-verified-only)` 占位，现在改为真实调用上游 `unixnu` 驱动；占位符已全部移除
（`grep -c TODO scripts/macos/build-macos.sh` 为 0）。**未验证的是结果，不是实现**：本机没有
macOS，因此"这些命令在真机上产出可启动镜像"这一点记为 UNVERIFIED，而不是写成"实现缺失"。
契约、输入校验、拒绝路径与发布守卫在 Windows 本机可执行、可测试；
`tests/acceptance/test_shell_script_syntax.py` 对每个 `scripts/**/*.sh` 跑 `bash -n`，
`tests/acceptance/test_p0p1_gate_main.py` 逐步钉住驱动调用的参数与顺序。
