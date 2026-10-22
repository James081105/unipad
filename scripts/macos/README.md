# scripts/macos/

macOS 专用入口（PRD §4.1、§8.1）。Xcode、Apple SDK 与磁盘工具只存在于 macOS，
因此这些脚本**只在 macOS runner 上执行**，Windows 本机绝不运行。

| 脚本 | 职责 | 硬性规则 |
| --- | --- | --- |
| `build-macos.sh` | 按冻结版本构建指定阶段，产出带逐文件 sha256 的 `artifact-manifest.json` | 输入必须 frozen；拒绝移动分支构建；拒绝 HVF/TCG 混淆；绝不上传完整系统镜像或私有数据（PRD 8.2 步骤 4） |
| `prepare-image.sh` | IPSW/RootFS 准备（校验 sha256、解包、生成本地清单） | 只写 `local-private/` 之外的调用方指定目录；绝不写入仓库；产物绝不提交 |

Windows 侧通过 GitHub run ID 下载对应产物并核验后运行（PRD 8.2 步骤 5–6）。
上游 Apple 工具相关步骤标注 `TODO(macos-only)`，其实现在现场（macOS）验证，
Windows 代码阶段只保证契约、输入校验与发布守卫可执行、可测试。
