# 天文台观测遥测采样与恢复工作台

这是一个只依赖 Python 3 标准库、`sqlite3` 和 `argparse` 的本地可重复实现。它从确定性生成的观测工程出发，完成 Span 树还原、关键路径、头部/尾部采样、失败根因检索、按作业保留量账目、策略局部重判和 SQLite 中断恢复。

## 快速开始

环境：Linux / WSL Ubuntu，Python 3.10+。不需要第三方包、网络或外部服务。

```bash
# 默认 seed，输出完整结果 JSON
python3 main.py

# 固定 seed，保存原始工程与完整结果
python3 main.py generate --seed 7 --output artifacts/workload.json
python3 main.py run --seed 7 --output artifacts/result.json

# 分别查看还原树、采样决策和保留量账目
python3 main.py restore --seed 7 --output artifacts/restoration.json
python3 main.py sample --seed 7 --output artifacts/sampling.json
python3 main.py ledger --seed 7 --output artifacts/ledger.json

# 运行全部测试
python3 -m unittest -v
```

所有命令输出规范化 JSON：键排序、UTF-8、两空格缩进，并以单个换行结尾。同一 seed 的原始工程和所有评估结果逐字节一致。

## 命令

| 命令 | 用途 |
| --- | --- |
| `run` | 完整还原、采样决策、失败分析、工程汇总。无子命令时默认执行。 |
| `generate` | 仅输出确定性原始工程。 |
| `restore` | 输出 Span 树、诊断、缺失节点和关键路径。 |
| `sample` | 输出每条链的头部/尾部采样决策与依据。 |
| `query` | 按 Trace、作业、工程和时间区间检索调用链及失败根因。 |
| `ledger` | 输出按作业的保留 Span 数、链路数、属性字节和过期标记。 |
| `recheck` | 调整采样率、尾部阈值或保留期，执行局部重判并与全量重判比对。 |
| `persist` | 用 SQLite 逐链事务提交；可模拟中断。 |
| `resume` | 从 SQLite 已提交状态继续，最终结果与一次性运行一致。 |

通用选项可放在子命令前或后：

```bash
--seed INTEGER
--json
--output FILE
--load-workload FILE
```

`--load-workload` 接受 `generate` 输出的工程 JSON，也可人工修改后重新评估。`--dump-workload FILE` 会在执行命令前另存原始工程。`--generate-only` 等价于 `generate`。

## 确定性样本

每个 seed 生成 3 份观测工程，每份 7 条调用链：

- 5 个曝光作业，跨 `telescope-control`、`camera-control`、`camera-readout`、`data-archive`、`object-store`、`catalog`。
- 2 个归档作业，跨 `data-archive`、`object-store`、`catalog`、`monitor`。

每个工程包含：

```json
{
  "project_id": "obsproj-...",
  "time_origin": {
    "window_start_us": 0,
    "window_end_us": 0,
    "now_us": 0
  },
  "jobs": [],
  "chain_definitions": [],
  "events": [],
  "policy": {},
  "edge_cases": {}
}
```

`chain_definitions[*].nodes` 给出预期的完整调用链，包括 `span_id`、`parent_span_id`、服务、操作、起止时刻、状态和属性。`events` 是按逻辑时刻交错的事件流，包含 `trace_id`、`span_id`、父 Span、作业、事件时刻、状态和属性；事件序号在工程内唯一且递增。

确定性边界样本的 ID 放在 `projects[0].edge_cases` 和 `projects[1].edge_cases` 中，包括：

- 错误恰好位于链路末端、且头部已丢弃、最终由尾部错误规则挽救；
- 根 Span 耗时恰好等于声明阈值，严格判定下不触发长耗时补充采样；
- 重复 Span 事件；
- 父 Span 事件缺失；
- 父字段完全缺失；
- Span 将自身声明为父节点。

生成器使用内部 SplitMix64 和 SHA-256，不依赖 Python 全局随机状态、当前时间、区域设置或文件系统顺序。

## 还原口径

### 去重与排序

同一 Trace 内按 `(sequence, event_time_us, event_id)` 选择第一条事件；后续同 `span_id` 事件不参与树和账目计数，并产生 `DUPLICATE_SPAN_IGNORED`。

### 异常父关系

- 缺少 `parent_span_id` 字段：该 Span 作为根，诊断 `PARENT_FIELD_MISSING`。
- `parent_span_id` 为 `null` 或空字符串：正常根 Span。
- 父 Span 最终未出现：建立确定性的合成缺失父节点 `__missing-parent__:<id>`，真实 Span 挂在它下面，诊断 `PARENT_NOT_FOUND`；合成节点不参与关键路径耗时。
- 父指向自身：断开为根，诊断 `SELF_PARENT_BROKEN`。
- 父链成环：按字典序最小的环内 Span 断开为根，诊断 `PARENT_CYCLE_BROKEN`。
- 有多个真实根：保留全部根，诊断 `MULTIPLE_TRACE_ROOTS`。

`chain_definitions` 中存在但事件流中未出现的节点列入 `expected_missing_nodes`。失败链的 `downstream_missing_nodes` 只包含首个错误 Span 在定义中的后代中实际缺失的节点。

### 关键路径

对每一条“根到叶”路径，权重为路径上所有真实 Span 的 `duration_us` 之和；取总耗时最大的路径。平局时选择 Span ID 序列字典序最小者。输出同时给出：

- `span_ids`、`spans`、`leaf_span_id`；
- 每个 Span 的 `duration_us` 与累计 `cumulative_duration_us`；
- 按服务聚合的 `by_service`；
- `total_duration_us`（路径耗时和）和 `elapsed_us`（路径首尾时间跨度）。

## 采样口径

### 头部采样

入口标记取自链路定义或根 Span 属性：

- `KEEP` / `SAMPLE` / `TRUE` / `1`：直接保留。
- `DROP` / `FALSE` / `0`：头部丢弃，但仍允许尾部挽救。
- `AUTO` 或缺失：执行比例规则。

比例规则：

```text
bucket = first_64_bits(SHA-256("head-sampling-v1", seed, project_id, trace_id)) mod 10000
head_kept = bucket < head_sampling_rate_basis_points
```

决策中的 `ratio_ordinal` 是该工程内按 `(trace_start_us, trace_id)` 排序后的 1 基链路序号。`head.marker` 和 `head.marker_source` 给出入口标记来源，`head.reason_code` 和 `head.bucket` 给出保留或丢弃依据。

### 尾部补充采样

尾部检查组会检查所有链，不仅已由头部保留的链；这样错误链不会因固定比例采样永久丢失。

- 任一 Span 状态属于 `tail_error_statuses`（默认只含 `ERROR`，大小写不敏感）即命中错误规则。
- 任一 Span 的耗时满足 `tail_duration_comparison`（默认 `gt`）与阈值时命中长耗时规则。
- 默认严格 `duration_us > threshold_ms * 1000`；耗时恰好等于阈值不触发。

最终保留：

```text
final_retained = head_kept OR tail_kept
```

因此不会出现“某条链既被最终保留又被最终丢弃”的矛盾。头部丢弃但尾部保留的链带 `final.tail_rescued=true`，并记录 `TAIL_ERROR` / `TAIL_DURATION` 理由、命中 Span 和作业。

## 检索口径

`query` 先过滤，再聚合：

```text
trace_start_us >= from_us 且 trace_start_us < to_us
```

可同时使用 `--trace-id`、`--job-id`、`--project-id` 和时间区间。默认只返回最终保留的链；加 `--include-dropped` 可显式查看丢弃链。聚合只使用过滤后的链，不会混入同时间窗口的其他作业。

失败定位取起始时刻最早、再按 Span ID 排序的首个 `ERROR` Span，输出：

- 首个错误 Span 及其服务、操作、状态、错误属性和是否观测叶节点；
- 定义中存在但事件流缺失的下游节点；
- 整条链的关键路径及按服务耗时分解。

## 保留量账目

`ledger` 对每个作业输出：

- `retained_trace_count`：最终保留链路数；
- `retained_span_count` / `span_count`：保留链中去重后的真实 Span 数；
- `attribute_bytes`：保留 Span 的 `attributes` 规范化 JSON 的 UTF-8 字节数；
- `expired_trace_count`、`expired_span_count`、`expired_attribute_bytes`；
- `over_retention` 与 `over_retention_trace_ids`。

过期判定：

```text
trace_start_us + effective_retention_us <= as_of_us
```

有效保留期优先取策略中的 `retention_overrides_us[job_id]`，否则取作业声明 `retention_us`。`--now-us` 可覆盖工程 as-of 时刻。

`trace_decisions` 是逐链明细，总额只从该明细聚合。`reconciliation.all_equal=true` 表示 Span 数、链路数和属性字节量三条账目全部与逐链采样决策一致。

## 策略调整与局部重判

策略文件示例：

```json
{
  "head_sampling_rate_basis_points": 7500,
  "tail_duration_threshold_ms": 1500,
  "tail_error_enabled": true,
  "retention_hours_by_job": {
    "EXP-...": 24
  }
}
```

```bash
python3 main.py recheck --seed 7 --patch policy.json
python3 main.py recheck --seed 7 --head-rate-bps 8000
python3 main.py recheck --seed 7 --duration-threshold-ms 1500 --no-tail-errors
python3 main.py recheck --seed 7 --retention-hours 'EXP-...=24'
```

局部重判只重算受影响链：

- 头部比例变化只影响 `AUTO` 链；显式 `KEEP`/`DROP` 链复用原决策。
- 错误规则变化只影响可能出现错误触发集合变化的链。
- 耗时规则变化只影响旧、新阈值下慢 Span 集合或理由元数据可能变化的链。
- 仅保留期变化不重算采样，只更新账目相关字段。

输出包含：

- `evaluated_trace_ids` / `reused_trace_ids`；
- `change_categories`；
- 前后账目 `before_ledger` / `after_ledger` 和 `ledger_diff`；
- `consistency.local_equals_full`、`ledger_equal`、`all_equal`。

`consistency.all_equal=true` 是局部重判与全量重判逐项一致的硬校验。

## SQLite 持久化与中断恢复

```bash
# 正常一次跑完
python3 main.py persist --seed 7 --db artifacts/observatory.sqlite3

# 提交 5 条链后模拟中断，返回码 130
python3 main.py persist --seed 7 --db artifacts/observatory.sqlite3 \
  --interrupt-after 5

# 从已提交状态续算
python3 main.py resume --seed 7 --db artifacts/observatory.sqlite3

# 可选：同时输出确定性的 SQLite 状态摘要
python3 main.py resume --seed 7 --db artifacts/observatory.sqlite3 --include-storage
```

每条链在独立事务中写入 `processed_traces`，主键为 `trace_id`。事务包含：

- 项目、作业、链路时间、Span 数、属性字节数和最终保留标记；
- 完整还原树与诊断（`trace_json`）；
- 完整采样决策及决策摘要。

重启时先读取已提交 `trace_id` 并跳过，使用 `INSERT OR IGNORE` 防止重复计数。SQLite 事务保证“写入但未提交”的数据在中断后回滚。默认情况下，`persist` / `resume` 直接输出与一次性 `run` 逐字节相同的结果；`--include-storage` 才会附加 SQLite 状态摘要。再次执行 `resume` 不会新增链路。

实际 SIGINT、`--interrupt-after` 模拟中断和事务级异常都走同一恢复路径。`--commit-delay-ms` 可用于在外部信号测试中拉长提交窗口。

持久化策略调整：

```bash
python3 main.py recheck --seed 7 \
  --db artifacts/observatory.sqlite3 \
  --duration-threshold-ms 1500
```

策略补丁历史写入 SQLite 元数据；仅受影响的链路行被更新，写入前必须通过局部等于全量的一致性校验。

## 主要输出字段

完整 `run` 结果：

```text
format
seed
workload_sha256
result_sha256
semantics
projects[]:
  project_id
  policy
  time_origin
  jobs[]                    # 有效保留期与过期时刻
  traces[]:
    trace_id, job_id, job_kind, observation_id
    trace_start_us, retention_us, retained
    restoration              # tree, flat_spans, diagnostics, critical_path
    sample_decision          # head, tail, final, reasons
    failure_analysis         # first error, missing downstream, critical path
  diagnostics[]
  summary
```

## 测试覆盖

`python3 -m unittest -v` 覆盖：

- 同 seed 生成与 CLI 输出逐字节一致；
- 树还原、重复事件、缺失父、缺失父字段、自环和多根诊断；
- 关键路径耗时与分解；
- 头部比例、显式标记、尾部错误挽救；
- 耗时恰好等于阈值不触发，低 1ms 后触发；
- 首个错误与下游缺失节点；
- 作业/时间过滤不串链；
- 账目与逐链决策守恒；
- 比例、阈值、错误开关、比较符和保留期调整的局部重判等于全量；
- SQLite 中断、恢复、重复执行不重复计数。
