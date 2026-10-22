# runs/

每个实验一个子目录 `runs/<experiment-id>/`，保存 PRD §9.2 证据最小集合：
`experiment.json`、`host-inventory.json`、`artifact-manifest.json`、实际 QEMU 参数、
固件/串口日志、客体日志、QMP 日志、带时间戳截图、`verification.json`。

运行产物不入 Git（见 `.gitignore`）。日志不得包含账号令牌、签名私钥与用户文档。
