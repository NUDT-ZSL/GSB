# 边缘缓存一致性教学与排障组件

这是一个仅使用 Python 标准库实现的本地边缘缓存一致性演示系统。内容源和三个边缘节点运行在回环 TCP 高位端口上，使用 JSON Lines 通信；SQLite 保存策略/内容双版本账本；JSON 快照支持版本识别、整体替换恢复和失效传播重放。

系统面向 Windows 本地 Python 3.11+，不依赖 Docker、外部站点或第三方包。

## 功能概览

- 确定性内容源：JSON 声明基础内容、ETag、字节内容和 `concat` / `sha256_concat` / `json_manifest` 派生规则。
- 三个独立节点进程：每个节点按策略组成缓存键，持久化 ETag、条目版本、TTL 锚点和剩余 TTL。
- 严格配置校验：TTL 正整数、键字段唯一且包含 `path`、TTL 覆盖全部路径、派生引用存在且无环。
- 稳定失效传播：触发路径加全部直接/间接下游按字典序稳定的拓扑顺序处理。
- 双版本账本：全局单调递增 `ledger_version`，并绑定 `policy_hash` 与 `content_set_hash`；陈旧版本操作返回 `E_VERSION`。
- 确定性逐出：支持 `lru`、`fifo`、`ttl`；容量逐出与 TTL 过期均写逐出事件。
- 快照：保存账本版本、策略、内容版本集合、派生关系、缓存、逐出与传播轨迹；版本不一致时全量替换；旧快照归档为只读备份。
- 强杀恢复：写入先落持久化 intent；未提交 intent 恢复为 `unknown` 并要求重跑；已提交条目从账本恢复。
- 重放校验：从快照内容图重新计算每个传播批次，逐项比较保存的有序路径集合；不可复现返回 `E_REPLAY` 和具体依据。
- Tkinter 诊断工作台：稳定排序展示缓存、TTL、逐出、传播链、账本版本和内容派生关系，并支持演示操作。

## 目录

```text
edgecache/
  config.py         策略、内容模型、校验、派生计算、稳定拓扑排序
  ledger.py         SQLite 账本、版本事务、缓存/逐出/传播/写入意图
  snapshot.py       快照校验、只读归档、整体替换恢复、重放校验
  source.py         内容源核心与 JSON-line TCP 入口
  node.py           节点缓存核心、恢复与 JSON-line TCP 入口
  controller.py     源站/节点 TCP 编排
  orchestrator.py   多进程、端口预检、优雅/强制终止
  demo.py           三阶段可重复演示与错误注入
  workbench.py      Tkinter 诊断工作台
samples/
  content.json      基础内容与两级派生关系
  content_cycle.json 派生环样本
  policy.json       v1：path+tenant，容量 4，LRU
  policy_v2.json    v2：path+tenant+encoding，容量 2，FIFO
  policy_broken.json 重复键字段/TTL=0 的坏策略
tests/              24 个标准库 unittest
run_demo.py         Windows 友好演示入口
run_workbench.py    Tkinter 入口
run_source.py       单源站入口
run_node.py         单节点入口
artifacts/verification/ 实际运行输出、日志和快照证据
```

## Windows 快速开始

确认 Python 与 Tkinter：

```powershell
python --version
python -c "import sqlite3, tkinter; print(sqlite3.sqlite_version, tkinter.TkVersion)"
```

运行完整演示（默认回环端口 `28180` 和 `28181..28183`）：

```powershell
python run_demo.py --base-port 28180
```

也可直接双击/执行 `run_demo.bat --base-port 28180`。

或：

```powershell
python -m edgecache.demo --base-port 28180 --state-dir runtime\demo
```

演示按顺序执行：

1. 首次冷启动填充：每个节点写入 6 个请求，容量 4，产生 2 个 LRU 逐出；执行命中并观察 TTL 递减。
2. 内容演进：`/assets/base.css` 从 `"base-v1"` 演进为 `"base-v2"`，稳定拓扑顺序传播到 `/assets/app.css`、`/assets/app.manifest.json`，各节点失效并重填。
3. 策略变更：切换到 `policy-v2`（新键字段、TTL、容量 2、FIFO），清空旧条目并整体重填。
4. 附加一致性校验：恢复阶段 2 快照，证明版本差异下全量替换；强杀 `edge-c` 和源站，验证未知写入及账本恢复。

错误注入：

```powershell
python run_demo.py --scenario bad-policy
python run_demo.py --scenario cycle
python run_demo.py --scenario port-occupied --base-port 28190
```

三个场景分别以进程码 `21`、`22`、`20` 和业务码 `E_POLICY`、`E_CYCLE`、`E_PORT` 退出。端口场景使用结构日志记录占用，不做任何备用端口重试。

## Tkinter 工作台

```powershell
python run_workbench.py --base-port 28280 --state-dir runtime\workbench
```

也可执行 `run_workbench.bat`。

工作台提供：

- 启动冷缓存、刷新、内容演进、应用策略 v2、捕获快照、恢复最近快照、强杀并恢复 `edge-c`。
- 五个稳定排序视图：缓存条目、逐出事件、失效传播链、账本版本、内容与派生关系。
- 缓存视图显示节点、路径、完整缓存键、ETag、条目版本、剩余 TTL、原始 TTL 与账本版本。

当前验证环境是无 Tk 的 WSL，无法实际打开窗口；入口已通过编译和模型层测试，Windows 官方 Python 安装包自带 Tkinter。

## 手动启动进程

先启动内容源；空账本时读取策略与内容文件，已有账本时只从 SQLite 恢复：

```powershell
python run_source.py --port 28180 --db runtime\manual\ledger.sqlite3 `
  --policy samples\policy.json --content samples\content.json --log runtime\manual\logs\source.jsonl
```

再启动节点：

```powershell
python run_node.py --node-id edge-a --port 28181 --source-port 28180 `
  --db runtime\manual\ledger.sqlite3 --log runtime\manual\logs\edge-a.jsonl
```

节点拉取示例：

```powershell
python -c "from edgecache.tcp import rpc; print(rpc('127.0.0.1',28181,{'op':'fill','ledger_version':1,'path':'/index.html','key_values':{'tenant':'public','encoding':'identity'}}))"
```

所有服务只绑定 `127.0.0.1`。端口冲突直接返回 `E_PORT`，不会尝试其他端口。

## JSON-line 协议

请求和响应均为单行 UTF-8 JSON。成功响应带 `"ok": true`，业务错误形如：

```json
{"ok":false,"error":{"code":"E_VERSION","message":"cache operation mixed ledger versions","location":"request.ledger_version","details":{"expected":1,"current":2}}}
```

源站操作：`ping`、`status`、`catalog`、`content`、`evolve`、`shutdown`。

节点操作：`ping`、`status`、`state`、`query`、`fill`、`fill_many`、`refresh`、`reset_policy`、`simulate_orphan_write`、`shutdown`。

## 数据模型

### 内容声明

基础条目必须声明 `path`、`etag`、`body`；`body_encoding` 支持 `utf-8` 或 `base64`。派生条目声明 `derived.sources` 与 `derived.rule`，其内容与 ETag 由上游确定计算；派生条目也可声明期望 ETag，加载时会与计算结果强校验，不一致返回 `E_POLICY`。

样本派生链：

```text
/assets/base.css ─┐
                   ├─> /assets/app.css ─┐
/assets/theme.css ─┘                     ├─> /assets/app.manifest.json
/index.html ─────────────────────────────┘
```

### 缓存策略

策略包含 `policy_version`、`key_fields`、`ttl_seconds_by_path`、`node_capacity`、`eviction_rule`。支持的键字段是 `path`、`tenant`、`encoding`。缓存键是策略声明顺序的规范化 JSON 数组。

### 条目

每个条目持久化：

- `etag`：内容版本。
- `entry_version`：该路径内容版本变化时递增，由全局 `path_state` 分配。
- `written_mono_ms`：写入时的单调时钟毫秒。
- `ttl_ms`、`remaining_ttl_ms`、`ttl_anchor_mono_ms`：TTL 与可查询剩余量。
- `last_access_mono_ms`：LRU 依据。
- `ledger_version`、`policy_hash`、`content_set_hash`：防止跨版本命中。

## 一致性机制

### 双版本账本

每次策略变更、内容演进或快照恢复都创建新的单调账本版本。缓存查询和写入必须携带当前 `ledger_version`；条目同时绑定策略哈希和内容集合哈希。任一不匹配返回 `E_VERSION`，不会被当作正常命中。

### 派生失效传播

内容演进在单个 SQLite 事务中完成内容状态、路径条目版本、传播批次和轨迹写入。传播集合从触发路径沿 `dependents` 图求可达集合，再用最小堆做稳定拓扑排序，因此不受 JSON 对象或字典遍历顺序影响。节点严格按传入顺序失效旧条目并重填新 ETag。

### 写入意图与强杀恢复

节点在持久化写事务前提交 `pending` intent；缓存写入与 `committed` 状态在同一事务提交。进程被强杀后，重启会把遗留 `pending` 标成 `unknown`，报告 `rerun_required: true`。已提交缓存只从 SQLite 读取，不依赖进程内存。源站已有账本时完全忽略启动文件；演示中通过传入不存在的策略/内容路径验证这一点。

### 快照与重放

快照包含账本版本、策略版本、内容版本集合、完整策略/内容/派生关系、路径条目版本、缓存、逐出、缓存事件、写入意图和传播批次/轨迹/节点结果，并以规范化 JSON SHA-256 校验。

恢复流程：

1. 校验 JSON、摘要、策略/内容哈希和内容版本集合。
2. 从内容图重算每个传播批次的有序路径与关系，逐项比对；失败返回 `E_REPLAY`，附带批次、期望、实际、缺失和多余路径。
3. 比较快照与当前账本、策略、内容版本，生成版本差异报告。
4. 删除全部当前缓存条目，插入快照条目，替换内容与派生关系，并创建新的单调账本版本；不做按缓存键的增量合并。
5. 保存新快照时，旧 `latest` 复制为只读 `backup-ledger-*.json`。

## 错误码

| 业务码 | 进程码 | 含义 |
| --- | ---: | --- |
| `E_PORT` | 20 | 回环端口占用/不可绑定，无备用端口重试 |
| `E_POLICY` | 21 | 策略或内容声明不合法，整体拒绝加载 |
| `E_CYCLE` | 22 | 派生关系成环 |
| `E_VERSION` | 23 | 查询/写入混用了两个账本版本 |
| `E_REPLAY` | 24 | 快照摘要或失效传播无法复现 |
| `E_NOT_FOUND` | 25 | 内容路径不存在 |
| `E_PROTOCOL` | 26 | JSON-line 协议请求不合法 |
| `E_STATE` | 27 | 账本未初始化或恢复状态不合法 |

## 测试

```powershell
python -m unittest discover -s tests -v
```

测试覆盖配置校验、派生 ETag 校验、环检测、稳定拓扑、TTL 递减/过期、LRU/FIFO、条目版本递增、`E_VERSION`、未知写入恢复、策略整体替换、快照只读备份、全量恢复、`E_REPLAY`、JSON-line TCP 和 Tkinter 模型层稳定排序。

本次实际执行结果见 [VALIDATION.md](VALIDATION.md)，完整输出在 `artifacts/verification/`。
