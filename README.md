# 确定性本地数据库连接池管理引擎

本项目在空白工作区中实现了一个不依赖真实数据库的连接池引擎。它使用本地 JSON
样本、注入式逻辑时钟、SQLite 事务和本地回环回显服务，复现连接申请、租约发放、
排队、空闲回收、算法切换、崩溃恢复和多进程幂等提交。

运行时代码只使用 Python 标准库；`numpy` 与 `pytest` 用于任务准备和测试环境。

## 快速开始（Windows）

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -e .
```

确认 SQLite：

```powershell
python -c "import sqlite3; print(sqlite3.sqlite_version)"
```

启动两个本地回环探测端点：

```powershell
python tools\echo_server.py --listen 127.0.0.1:19091 --listen 127.0.0.1:19092
```

运行完整确定性演示：

```powershell
python scripts\demo.py
```

演示脚本会自动启动/停止回环服务，执行 `load/acquire/release/resume/status/verify`，
连续运行两次 `verify` 并比较输出。结果同时写入 `.demo\verification.json`。

## CLI

所有业务命令输出稳定排序的 JSON；失败时向 stderr 输出带业务错误码的 JSON。

```powershell
# 解析、校验并初始化
python -m dbpool load `
  --endpoints samples\endpoints.json `
  --config samples\pool_config.json `
  --trace samples\trace.json `
  --db .demo\pool.db

# 从已加载轨迹中按申请序号申请
python -m dbpool acquire --db .demo\pool.db --seq 1 --clock-ms 0

# 释放（--close 表示主动关闭物理连接）
python -m dbpool release --db .demo\pool.db --seq 1 --clock-ms 20 --close

# 崩溃后恢复；可选推进逻辑时钟
python -m dbpool resume --db .demo\pool.db --clock-ms 21

# 稳定状态报告；--at-clock 会先推进时钟并执行到期回收/队列派发
python -m dbpool status --db .demo\pool.db --at-clock 100

# 两次自动模式重放 + exact/tiered 强制模式等价性验证
python -m dbpool verify `
  --endpoints samples\endpoints.json `
  --config samples\pool_config.json `
  --trace samples\trace.json `
  --transport loopback
```

`verify --transport simulated` 可做无网络的确定性单元验证；默认 `loopback`
会真实连接本地回显服务。

## 样本格式

- `samples/endpoints.json`：端点名、`tcp://loopback:port` 地址、每端点最大连接数、
  建链逻辑耗时、健康状态、支持的池标签。
- `samples/pool_config.json`：总连接上限、每租户配额、空闲超时、最小空闲数、
  单轮逻辑预算、时钟步进、桶宽、切换容差、租户到池标签映射。
- `samples/trace.json`：申请序号/版本、租户、池标签、申请逻辑时钟、持有逻辑时长、
  是否生成释放事件，以及最终清理时钟。

申请唯一键为 `(seq, version)`。相同键的重复提交必须内容一致，并返回第一次提交的
租约或排队结果，不会重复发放。

## 关键行为

- **双重容量约束**：同时检查总物理连接上限、每端点最大连接数和每租户配额。
- **原子账本**：连接行、租约行、请求水位、空闲摘要、配额计数和状态版本在同一
  SQLite `BEGIN IMMEDIATE` 事务中提交。
- **确定性调度**：精确模式按空闲时长严格排序；分层模式从最老桶向最新桶扫描，
  桶内仍按精确空闲时长排序，因此两种模式选择出的连接序列逐位一致。
- **自适应切换**：使用空闲连接数、池标签跨度、队列深度和占用桶数估算两种模式的
  成本，带迟滞容差切换；切换事件记录触发条件、时钟、成本、被否决模式及原因。
- **确定性回收**：按 `(idle_since_ms, endpoint_name, conn_id)` 顺序回收，且绝不
  回收 `IN_USE` 连接；原因分为 `IDLE_TIMEOUT`、`QUOTA_SHRINK`、`ACTIVE_CLOSE`。
- **崩溃恢复**：建链前先提交 `OPENING` 意图；最终事务把租约、连接和配额一起提交。
  恢复时识别遗留 `OPENING` 意图和孤立/缺失连接，从明细重算账目，不做增量重复扣减。
- **多进程**：写路径使用跨平台文件锁和 SQLite 事务；同一申请只发放一次，重复释放
  返回首次释放回执，读取方通过 WAL 只能看到完整已提交状态。

## 业务错误码

| 错误码 | 场景 |
| --- | --- |
| `E_INVALID_JSON` | 样本不是合法 UTF-8 JSON 对象 |
| `E_DUPLICATE_ENDPOINT` | 端点名重复 |
| `E_DUPLICATE_APPLICATION` | 相同申请序号/版本重复 |
| `E_INVALID_LOOPBACK_ADDRESS` | 非回环地址、无端口或非法端口 |
| `E_INVALID_ENDPOINT` / `E_INVALID_APPLICATION` | 字段类型或取值非法 |
| `E_INVALID_CONFIG` / `E_POOL_BOUNDARY` / `E_INVALID_CLOCK_STEP` | 池配置或边界非法 |
| `E_TENANT_POOL_LABEL` / `E_UNKNOWN_POOL_LABEL` | 租户标签映射或端点能力不匹配 |
| `E_CONFIG_VERSION_ROLLBACK` | 池配置版本回退 |
| `E_CONFIG_VERSION_CONFLICT` | 同版本配置内容变化 |
| `E_SAMPLE_DIGEST_MISMATCH` | 样本摘要或 `--expect-digest` 不匹配 |
| `E_ENDPOINT_REFUSED` | 回环端点拒绝连接 |
| `E_ENDPOINT_TIMEOUT` | 连接或探测超时 |
| `E_BUILD_BUDGET_EXCEEDED` | 单次建链逻辑耗时超过单轮预算 |
| `E_MALFORMED_PROBE` | 回显探测响应畸形 |
| `E_APPLICATION_CONFLICT` | 同申请键内容不一致 |
| `E_CLOCK_REGRESSION` | 逻辑时钟倒退 |
| `E_LEASE_NOT_FOUND` | 释放不存在的租约 |

## 故障注入

测试可通过环境变量在指定点强制终止进程：

```powershell
$env:DBPOOL_FAULT_POINT = "after_connect_before_commit"
python -m dbpool acquire --db .demo\pool.db --seq 1 --clock-ms 0
```

支持点包括 `after_intent_before_connect`、`after_connect_before_commit`、
`after_rows_before_commit`、`after_commit_before_ack`、
`release_after_state_before_commit`、`resume_after_repair_before_commit`。
退出码默认 91，可用 `DBPOOL_FAULT_EXIT_CODE` 覆盖。

## 验证

```powershell
python -m pytest -q
python scripts\demo.py
```

实际验证记录见 `docs/verification.md`；算法与事务设计见 `docs/design.md`。
