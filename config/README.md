# config/

拟建配置（PRD §9）。模板已随仓库提供；`null` 字段表示 M0 待冻结项，禁止在无证据情况下填写。

| 文件 | 内容 |
| --- | --- |
| `sources.lock.json` | UniXNU / 设备仓库 / 子模块 / feeds / IPSW / 工具固定版本。`frozen=true` 前不得据此构建 |
| `machine.json` | 虚拟机器定义：CPU、内存、设备布局、时钟、显示、磁盘路径 |
| `budget.json` | 实验与成本上限。未授权付费时 Actions/macOS 构建保持 BLOCKED |
