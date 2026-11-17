# MVCC Visibility Workbench

面向 Windows 本地环境的 MVCC 可见性实验工作台。项目使用 Python 3.11+
标准库，无第三方运行时依赖；持久化使用本地 SQLite，桌面界面使用 Tkinter，
命令行与测试覆盖完整的确定性验收路径。

## 能力概览

- 单调事务号分配、一致性快照（`snapshot_xmin`、`snapshot_xmax`、活跃事务集合）。
- SQLite 物理多版本链：`xmin`、`xmax`、tombstone、双向指针与版本创建水位。
- 快速路径：内存增量版本索引 + 按会话状态版本整体失效的判定缓存。
- 权威路径：回到 `txn_events` 事务日志与 SQLite 版本链逐版本重放。
- 稳定候选链与逐步依据：`xmin` 提交状态、是否在快照内、`xmax` 状态与最终决策。
- 后台并发写入、最新快照查询、单次查询一致性快照与整数定点延迟指标。
- 命名快照保存、版本差异识别、事务表/版本链整体替换、历史判定重放。
- 强制终止后的启动恢复：未提交事务统一标记为回滚，不生成退出报告。
- 三类输入异常注入：`E_CHAIN`、`E_CACHE`、`E_TXN`，全部写入 JSONL 结构化日志。

## Windows 环境准备

1. 安装 Python 3.11 或更高版本的 Windows 官方发行包，并勾选 `tcl/tk and IDLE`。
2. 在项目根目录打开 PowerShell 或命令提示符。
3. 确认标准库：

```powershell
py -3 --version
py -3 -c "import sqlite3, tkinter; print(sqlite3.sqlite_version, tkinter.TkVersion)"
```

首次验收建议清空历史运行目录：

```powershell
py -3 -m mvcc_workbench reset --session-dir run
```

## 快速开始

运行确定性样本并生成完整验收报告：

```powershell
py -3 -m mvcc_workbench --session-dir run demo --output artifacts\verification.json
```

Windows 批处理入口：

```text
scripts\run_demo.bat
scripts\run_gui.bat
```

启动 Tkinter 工作台：

```powershell
py -3 -m mvcc_workbench --session-dir run gui
```

界面包含事务表、事务视角切换、快速查询、权威校验、候选链与依据、脚本编辑、
命名快照、后台提交并发上限和实时结构化日志。

## 常用 CLI

```powershell
# 清空并执行脚本
py -3 -m mvcc_workbench reset --session-dir run
py -3 -m mvcc_workbench --session-dir run run-script --script samples\deterministic_session.json

# 快速查询 / 快速与权威校验
py -3 -m mvcc_workbench --session-dir run query --txid 4 --table inventory --key sku-1
py -3 -m mvcc_workbench --session-dir run verify --latest --table inventory --key sku-1

# 保存、整体恢复与历史重放
py -3 -m mvcc_workbench --session-dir run save-snapshot --name baseline
py -3 -m mvcc_workbench --session-dir run restore-snapshot --name baseline --replay
py -3 -m mvcc_workbench --session-dir run replay-history

# 后台并发提交期间持续做双路径校验
py -3 -m mvcc_workbench --session-dir run background-check --concurrency 3 --commits 8

# 纯输入注入：只改快照文件副本
py -3 -m mvcc_workbench --session-dir run inject-snapshot --source run\snapshots\baseline.mvccsnap.json --output run\injected-chain.json --kind chain
py -3 -m mvcc_workbench --session-dir run restore-snapshot --file run\injected-chain.json

# 自动运行 chain/cache/txn 三种注入
py -3 -m mvcc_workbench --session-dir run anomaly-demo --source run\snapshots\baseline.mvccsnap.json
```

业务错误以 JSON 输出，进程退出码为 `2`，例如：

```json
{
  "ok": false,
  "error": {
    "code": "E_CHAIN",
    "message": "snapshot version pointers disagree",
    "details": {"table": "inventory", "key": "sku-1", "chain_position": 3}
  }
}
```

## 快照与查询语义

- 事务视角：事务在 `begin` 时固定活跃集合和 `snapshot_xmax`；之后其他事务提交不会改变该事务的可见值结论。
- `LATEST` 视角：以查询捕获的单一会话状态版本构造最新快照；活跃集合、事务状态和版本链都从同一水位读取。
- 候选链：只枚举该快照边界内可能参与判定的版本；`xmin >= snapshot_xmax` 的更新版本被排除，保证事务视角重复判定稳定。
- 快速路径与权威路径使用同一个已固定快照上下文；缓存键包含快照指纹和视图版本。任一会话版本变化都会清空全部缓存，不做逐条修补。
- 判定 JSON 使用排序键、紧凑分隔符的规范编码；历史记录保存规范字节的 SHA-256 等价内容，可直接逐字节比较。

详细设计见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)，实际验收结果见
[docs/VERIFICATION.md](docs/VERIFICATION.md) 和 `artifacts/verification.json`。

## 测试

```powershell
py -3 -m unittest discover -v
```

测试覆盖事务非法迁移、重复版本原子拒绝、链损坏、快照隔离、快速/权威字节一致、
命名快照整体恢复、历史重放、三类异常注入、强制恢复、定点延迟指标和后台并发查询。

## 项目结构

```text
mvcc_workbench/
  storage.py        SQLite 表结构、事务、版本链、历史与整体替换
  visibility.py     纯可见性判定、稳定依据、首个分歧定位
  index.py          增量版本索引与整体失效缓存
  engine.py         Workbench 服务、快慢路径、快照与重放
  script.py         JSON 事务脚本执行器
  background.py     可配置并发后台提交器
  inject.py         输入快照异常注入
  cli.py            命令行入口
  gui.py            Tkinter 桌面入口（延迟导入）
samples/            确定性事务与查询序列
tests/              标准库 unittest
docs/               架构与验收记录
artifacts/          实际验证报告
```

## 边界说明

- SQLite 写入在单个本地会话进程内通过写事务和服务状态锁串行化，以保证唯一确定结果；后台仍按配置的线程并发上限持续提交。
- 工作台面向单机实验，不提供跨进程同时写同一 SQLite 文件的协调协议。
- 当前 Linux 验收容器未安装 Tkinter，因此 CLI、持久化、并发与重放均已实测，GUI 完成无 Tk 环境下的导入、语法和伪控件构建冒烟验证；Windows 官方 Python 可直接运行界面。
