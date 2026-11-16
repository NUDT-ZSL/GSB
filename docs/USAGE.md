# CDN 边缘缓存一致性诊断工作台（cdnlab）

纯 Python 3.11 标准库实现（socket / threading / sqlite3 / tkinter），无第三方依赖、无容器。
目标平台为本地 Windows（官方 python.org 安装包自带 Tkinter），代码跨平台，Linux/WSL 同样可运行。

## 目录结构

- `cdnlab/origin.py` —— 源站模拟进程：确定性样本内容、ETag / Last-Modified、逻辑版本计数
- `cdnlab/edge.py` —— 边缘节点进程：按策略缓存、单一逻辑时钟 TTL、LRU/LFU/FIFO 逐出、回源校验、陈旧标记
- `cdnlab/config.py` —— 缓存策略 JSON 加载与强校验（非法配置整体拒绝，业务码 `E_CONFIG`，含错误位置）
- `cdnlab/orchestrator.py` —— 本地编排：回环高位端口启动 1 源站 + N 边缘，端口占用报 `E_PORT`，失败整体清理
- `cdnlab/session.py` —— SQLite 会话持久化（配置版本、请求/注入轨迹、逐出事件、一致性结论、快照）
- `cdnlab/replay.py` —— 快照恢复后的重放校验（分歧报 `E_REPLAY` 并给出首个差异请求序号）
- `cdnlab/workbench.py` —— Tkinter 诊断界面
- `cdnlab/demo.py` —— 四个可重复演示场景
- `samples/` —— 确定性样本内容、`cache_policy.json`、非法配置样例、注入参数样例
- `scripts/` —— `run_demo.sh` / `run_demo.ps1` / `run_workbench.ps1`
- `tests/test_cdnlab.py` —— 23 个单元/集成测试

## 业务码

| 码 | 含义 |
|---|---|
| `E_PORT` | 回环端口被占用或进程启动失败（已启动进程整体清理，不留孤儿） |
| `E_CONFIG` | 配置非法（TTL/容量非正整数、键字段重复或不支持、路径重复、逐出规则非法） |
| `E_STALE` | 注入源站内容更新 |
| `E_UPSTREAM` | 注入源站不可达；陈旧条目被显式标记返回 |
| `E_EVICT` | 注入逐出压力；逐出事件逐条记录 |
| `E_REPLAY` | 重放不可复现（配置版本不一致或首个差异请求序号） |

## 运行（Windows）

```powershell
# 前提：Python 3.11（含 Tkinter），关闭占用回环高位端口的其他程序
python -m cdnlab.demo                 # 顺序执行四个场景，输出结构化 JSON 总结
python -m unittest discover -s tests  # 测试套件
python -m cdnlab.workbench --db artifacts\demo\scenario4_restore_replay.sqlite
```

或使用脚本：`scripts\run_demo.ps1`、`scripts\run_workbench.ps1`。

## 可复现性设计

- 时间：编排器维护单一逻辑时钟 `tick`，TTL 剩余量 = `ttl - (tick - write_tick)`，不依赖墙钟。
- 版本：源站版本为逻辑计数器；ETag = sha1(path, version, body)；Last-Modified 由固定基准纪元推导。
- 注入：仅通过命令行/协议参数改变输入（内容、可达性、压力），不改动工作台源码。
- 快照：整体替换恢复（配置版本不一致时绝不增量合并）；重放前复位到轨迹起点基线再逐条比对。
