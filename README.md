# 本地慢查询诊断与改写引擎

这是一个仅依赖本地文件的确定性慢查询诊断引擎。它完成以下链路：

1. 校验 SQLite 样本库、慢查询采样流和统计快照/补丁。
2. 将 SQL 归一化为稳定模板，并按模板、快照版本聚合样本。
3. 枚举全表扫描与可用索引路径，生成执行计划画像。
4. 在并发上限和内存预算下并行生成画像，显式记录预算放弃。
5. 按注入逻辑时钟切分采样窗口、绑定统计快照并控制刷新范围。
6. 将采样水位、窗口、聚合、画像、任务和内存账目事务化写入 SQLite。
7. 进程被强制终止后，通过 `resume` 只重做未提交部分。
8. 以临时表 + 版本指针整体替换统计快照，并计算依赖失效闭包。

## 环境

- Python 3.11 或更高版本。
- 标准库：`sqlite3`、`hashlib`、`concurrent.futures` 等。
- 声明依赖：`numpy`、`pytest`。核心确定性计算不依赖系统墙钟。

本工作区实际验证环境：

```text
Python 3.12.13
numpy 2.5.5
pytest 9.2.0
```

Unix/macOS：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install numpy pytest
```

Windows PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install numpy pytest
```

`slowquery.py` 是普通 Python 入口，可直接在 Windows 控制台运行。

## 本地样本

```text
samples/
  rebuild_db.py                 确定性重建 sample.db
  sample.db                     已重建的 SQLite 样本库
  slow_queries.json             初始慢查询采样流
  slow_queries_after_patch.json 补丁生效后到达的采样流
  stats_snapshot.json           初始统计快照 version=1
  stats_patch.json              增量统计补丁 version=2
```

重建样本库：

```bash
python samples/rebuild_db.py
```

样本库包含 `customers`、`orders`、固定数据及 4 个普通索引。重建过程不使用随机数。
如果默认的 `samples/sample.db` 缺失，`load` 会在采样流和统计快照校验通过后自动调用同一重建函数；非法输入不会触发建库或状态写入。

## 快速演示

```bash
python samples/rebuild_db.py

python slowquery.py --state .slowquery/state.db load --reset
python slowquery.py --state .slowquery/state.db sample
python slowquery.py --state .slowquery/state.db plan \
  --max-workers 2 --memory-budget 1024 --memory-unit 8

python slowquery.py --state .slowquery/state.db snapshot
python slowquery.py --state .slowquery/state.db sample \
  --samples samples/slow_queries_after_patch.json
python slowquery.py --state .slowquery/state.db plan \
  --max-workers 3 --memory-budget 1024 --memory-unit 8

python slowquery.py --state .slowquery/state.db status
python slowquery.py --state .slowquery/state.db verify
```

重复执行相同的 `sample`、`snapshot` 或 `plan` 是幂等的。`verify` 成功时退出码为 0，输出中 `ok=true`；失败时退出码为 1。

## CLI

### `load`

校验全部输入后初始化状态库：

```bash
python slowquery.py --state STATE load \
  --db samples/sample.db \
  --samples samples/slow_queries.json \
  --stats samples/stats_snapshot.json \
  --window-ms 1000 \
  --reset
```

`--reset` 清空状态后重新初始化。非重置模式下，如果已有状态与输入模型/初始快照不同，返回 `ERR_STATE_CONFLICT`，不会覆盖已有结果。所有输入在打开/写入状态库前完成校验；非法输入不会创建状态库。

### `sample`

```bash
python slowquery.py --state STATE sample \
  --samples samples/slow_queries.json \
  --clock-ms 2700
```

- 采样序号必须严格递增且唯一。
- 逻辑时钟、耗时、扫描行数、返回行数必须是非负整数。
- SQL 必须是可归一化的只读 `SELECT`。
- 每个窗口作为一个 SQLite 事务提交；进程在窗口中退出时该窗口整体回滚。
- 已存在且内容相同的采样为幂等重复；同序号不同内容返回 `ERR_SAMPLE_CONFLICT`。
- 写入前先做整批只读预检，状态级冲突不会先写入部分新采样。
- 同一模板与同一快照版本已有画像后，再追加新采样返回 `ERR_WINDOW_SEALED`，从而保证“同一模板同一快照版本画像只提交一次”。

### `plan`

```bash
python slowquery.py --state STATE plan \
  --max-workers 2 \
  --memory-budget 64 \
  --memory-unit 8 \
  --clock-ms 2700
```

调度语义：

- 任务按 `(template_id, snapshot_version)` 稳定排序。
- 每个任务的内存单位为 `候选路径数 * memory-unit`。
- 只启动 `逻辑时钟 <= --clock-ms` 的聚合，形成基于注入时钟的刷新边界。
- `max-workers` 控制真实并发的 `ThreadPoolExecutor` 工作线程数。
- 内存预算按运行累计记账；任务第一次启动时扣费，恢复时不会重复扣费。
- 下一个待启动任务无法装入剩余预算时，显式把当前任务及其后的全部 `pending` 任务标为 `abandoned`，并写入 `ERR_MEMORY_BUDGET`、需求/剩余/已用预算。
- 已启动任务必须等待执行完毕；失败只标记 `failed`，不会事后标成超时。

任务阶段严格区分：

```text
pending     预算内尚未启动（可恢复）
running     已启动并已记账
committed   画像已与任务状态在同一事务中提交
failed      启动后计算/提交失败
abandoned   因预算不足主动放弃
```

### `resume`

```bash
python slowquery.py --state STATE resume
python slowquery.py --state STATE resume --skip-sample
python slowquery.py --state STATE resume --retry-abandoned --memory-budget 160
python slowquery.py --state STATE resume --retry-failed
```

默认先幂等重放初始采样流，再恢复最近一次计划。被终止进程遗留的 `running` 任务会回到 `pending`，但其已记账内存保持不变。已提交任务和画像直接跳过。`pending` 是“未启动”，`failed` 是“启动后失败”，`abandoned` 是“主动预算放弃”，三者不会混淆。

### `snapshot`

```bash
python slowquery.py --state STATE snapshot --patch samples/stats_patch.json
```

补丁裁决：

- 版本回退：`ERR_SNAPSHOT_ROLLBACK`。
- 同版本且字段一致：`no_op`，幂等。
- 同版本但内容不同：`ERR_SNAPSHOT_CONFLICT`。
- 更高版本且完整覆盖表集合或 `mode=replace`：`complete`。
- 更高版本且只覆盖部分统计项或 `mode=merge`：`incremental`。

提交时先写入连接级临时表 `incoming_snapshot`，再整体插入不可变快照表并更新单行指针。版本、快照内容和指针在同一事务中可见。

失效闭包按实际候选路径重算比较，输出：

```json
{
  "changed_stats": [],
  "affected_templates": [],
  "aggregates_to_recompute": [],
  "profiles_to_recompute": [],
  "reusable_profiles": []
}
```

未受影响的旧画像不会被覆盖或删除。

### `status`

输出稳定排序 JSON，包括：

- 采样水位与逻辑时钟水位。
- 当前统计快照指针、全部快照版本及生效时钟。
- 窗口摘要、每模板聚合摘要。
- 画像任务的 `pending/running/committed/failed/abandoned` 阶段。
- 运行级内存预算、已记账、活动任务内存及预算放弃记录。
- 画像绑定快照版本和完整画像结果。

### `verify`

独立检查：

- 采样序号唯一严格递增、水位一致。
- SQL 重复归一化逐位一致。
- 每个采样窗口由逻辑时钟唯一计算。
- 采样绑定快照同时满足“补丁到达时已知”和“生效时钟已到”。
- 从原始采样串行重算全部聚合。
- 从聚合串行重算全部画像，并与并行持久化结果逐字段比较。
- 任务唯一键、内存账目、终态原因和画像完整性。
- 失效闭包摘要完整性。

## 输入格式

采样流：

```json
{
  "stream_version": 1,
  "samples": [
    {
      "seq": 1,
      "logical_clock_ms": 100,
      "sql": "SELECT id FROM orders WHERE customer_id = 7",
      "duration_ms": 900,
      "scanned_rows": 8,
      "returned_rows": 2,
      "index_hit": "idx_orders_customer_status"
    }
  ]
}
```

统计快照：

```json
{
  "version": 1,
  "effective_at_ms": 0,
  "covered_tables": ["orders"],
  "tables": {
    "orders": {
      "row_count": 600,
      "columns": {
        "status": {"ndv": 4}
      },
      "indexes": {
        "idx_orders_status": {
          "columns": ["status"],
          "unique": false,
          "prefix_selectivity": {
            "status": 0.25
          }
        }
      }
    }
  }
}
```

增量补丁使用同一表结构，额外允许 `"mode": "merge"`、只提供被更新的统计字段和部分 `covered_tables`。完整替换使用 `"mode": "replace"` 并提供完整表集合。索引列与 `prefix_selectivity` 的前缀必须与 `sample.db` 的实际结构一致。

## 归一化规则

归一化器是递归下降 SQL 解析器，不是简单字符串替换。当前接受只读 `SELECT`，支持：

- 字面量、参数、算术、比较、`AND/OR/NOT`、`BETWEEN`、`IN`、`LIKE`。
- `CASE`、`CAST`、常见函数、`JOIN ... ON/USING`、分组、排序、限制。
- 子查询形式的 `IN`/`EXISTS`。

模板结构语义：

- 字符串/数字字面量统一为 `literal` 节点，不保留值。
- `IN (1)`、`IN (1,2,3)` 归一为单个 `in_list_marker`。
- 关键字、标识符、空白、大小写和尾分号被规范化。
- 模板 ID 为 `sha256(canonical_json({normalizer: 1, structure}))`。
- `WITH`、`UNION/INTERSECT/EXCEPT` 等未支持形态返回 `ERR_SQL_PARSE`。

## 聚合规则

每个 `(template_id, snapshot_version)` 聚合：

- `sample_count`
- `p50/p90/p95/p99` 耗时
- `avg_scanned_rows`、`avg_returned_rows`
- 总扫描/返回行数
- 窗口集合、最小/最大序号和逻辑时钟
- 聚合摘要 `aggregate_digest`

分位数规则固定为：耗时升序排序后，在位置 `(n-1)*q` 做线性插值。浮点结果统一舍入到 12 位可表示值。

## 画像规则

每个模板枚举：

- 每张引用表的全表扫描路径。
- 每个首列出现在谓词中的索引路径；索引前缀按等值列和首个范围列确定。

估算来源优先级：

1. 快照中的索引前缀选择性。
2. 唯一索引的 `1/row_count`。
3. 索引整体选择性。
4. 列 NDV 推导。

画像记录：

- 所依赖快照版本、生效时钟和摘要。
- 候选路径、索引使用前缀、估算选择性和估算行数。
- 实测平均扫描/返回行数。
- 偏差比、偏差方向及首个偏差统计项来源。
- 最佳路径。
- 结果摘要 `result_digest`。

## 状态与恢复设计

状态库默认位于 `.slowquery/state.db`，启用 WAL、`BEGIN IMMEDIATE` 和完整同步。

关键原子边界：

- 每个窗口的采样、窗口摘要、模板聚合和水位在同一个事务提交。
- 任务 `pending -> running` 与内存扣费在同一事务提交。
- 画像插入与任务 `running -> committed` 在同一事务提交。
- 快照临时表、版本行和当前指针更新在同一事务提交。

因此进程在任意执行点被终止后：

- 未提交事务由 SQLite 回滚。
- 已提交采样不会被重复计数。
- 已提交画像不会被重复生成。
- 已记账但未提交的 `running` 任务恢复时重用原内存账目。

计划调度使用进程级文件锁串行化 scheduler leader；采样和补丁写入依靠 SQLite 唯一约束与写事务实现多进程幂等。

## 确定性终止测试钩子

通过环境变量注入进程退出点，退出码为 86：

```bash
# 首个任务已记账、尚未计算/提交时退出
SLOWQUERY_FAILPOINT=after_claim:1 \
  python slowquery.py --state STATE plan ...

# 计算完成、提交画像前退出
SLOWQUERY_FAILPOINT=before_profile_commit:1 \
  python slowquery.py --state STATE plan ...

# 第一个窗口事务提交前退出（整个窗口回滚）
SLOWQUERY_FAILPOINT=before_window_commit:1 \
  python slowquery.py --state STATE sample

# 第一个窗口提交后退出（该窗口保留，后续窗口重做）
SLOWQUERY_FAILPOINT=after_window:1 \
  python slowquery.py --state STATE sample

# 恢复
python slowquery.py --state STATE resume --skip-sample
```

PowerShell：

```powershell
$env:SLOWQUERY_FAILPOINT = "after_claim:1"
python slowquery.py --state STATE plan
Remove-Item Env:SLOWQUERY_FAILPOINT
python slowquery.py --state STATE resume --skip-sample
```

## 主要业务错误码

```text
ERR_INVALID_JSON
ERR_SAMPLE_SCHEMA
ERR_SAMPLE_SEQ
ERR_SAMPLE_SEQ_ORDER
ERR_SAMPLE_CLOCK
ERR_SAMPLE_METRIC
ERR_SAMPLE_CONFLICT
ERR_SQL_PARSE
ERR_NORMALIZATION
ERR_SNAPSHOT_SCHEMA
ERR_SNAPSHOT_ROLLBACK
ERR_SNAPSHOT_CONFLICT
ERR_SNAPSHOT_NOT_EFFECTIVE
ERR_SNAPSHOT_COMMIT
ERR_WINDOW_SEALED
ERR_DB_SCHEMA
ERR_STATE_NOT_INITIALIZED
ERR_STATE_CONFLICT
ERR_MEMORY_BUDGET
ERR_BUDGET_CONFIG
ERR_PLAN_TASK_NOT_FOUND
ERR_PLAN_TASK_NOT_CLAIMED
ERR_PROFILE_CONFLICT
ERR_PROFILE_IMMUTABLE
ERR_LOCK
```

CLI 错误输出到 stderr：

```json
{
  "ok": false,
  "error": {
    "code": "ERR_SAMPLE_SEQ",
    "message": "sample sequence numbers must be strictly increasing",
    "details": {}
  }
}
```

## 测试与实测结果

运行：

```bash
python -m pytest -q
```

本工作区实际结果：

```text
...................                                                      [100%]
19 passed
```

已覆盖：

- 字面量、大小写、空白、IN 列表长度归一化。
- 非法采样/指标在写状态前被拒绝。
- 采样与聚合幂等、固定分位数。
- 并行画像与单线程串行画像逐字段一致。
- 双预算放弃及显式重试。
- 任务中断恢复不重复扣费。
- 窗口提交前回滚、窗口提交后恢复。
- 增量补丁、重复补丁 no-op、版本回退拒绝、冲突拒绝。
- 完整替换与临时表指针切换。
- 未受影响画像保持不变、精确失效闭包。
- 快照到达版本与生效时钟双重绑定。
- 多进程并发采样、补丁和计划不产生重复结果。

本次端到端实测：

```text
load:     version=1, validated_samples=12
sample:   inserted=12, committed_samples=12, logical_clock=2700
plan #1:  completed, memory charged=72, 4/4 committed
verify #1: ok=true, samples=12, windows=3, aggregates=4, profiles=4

patch:    incremental version=2, affected_templates=3, reusable_profiles=1
sample #2: inserted=4, committed_samples=23
plan #2:  completed, 8/8 tasks committed
verify #2: ok=true, samples=16, windows=4, aggregates=8, profiles=8
```

预算验收单独使用 `memory-budget=40, memory-unit=8`：前 2 个任务提交，剩余 2 个任务以 `ERR_MEMORY_BUDGET` 主动放弃；把预算提高到 160 并用 `--retry-abandoned` 恢复后全部提交，`verify` 仍为 `ok=true`。
