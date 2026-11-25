# 技术说明

## 1. 模块边界

| 模块 | 职责 |
| --- | --- |
| `feature_diag/config.py` | v1/v2 配置合并后的严格规范化、参数校验、默认补全来源 |
| `feature_diag/sample.py` | PCG64 固定种子样本生成、CSV 行模型、输入摘要 |
| `feature_diag/engine.py` | 特征计划、前缀拟合、编码、交叉展开、方差/互信息选择、增量计算 |
| `feature_diag/cache.py` | 内容寻址编码缓存、选择快照、SQLite 阶段锁、陈旧结果检测 |
| `feature_diag/query.py` | 按 `row_id` 查询、预算检查、真实/逻辑时钟、进程池查询限制 |
| `feature_diag/snapshot.py` | v1 单文件、v2 分片+SQLite 元数据、校验和、互迁与回退拒绝 |
| `feature_diag/checkpoint.py` | 批次边界检查点、单调版本、内容校验和、恢复与优雅停止 |
| `feature_diag/report.py` | canonical 验收编排、稳定 JSON、错误注入矩阵、实际遥测 |
| `feature_diag/cli.py` | 所有可执行命令与顶层异常边界 |

## 2. 输入与特征语义

样本 CSV 包含：

- `row_id`
- 数值列 `age`、`income`、`score`
- 类别列 `region`、`segment`、`channel`
- 标签 `label`

数值列支持：

- `standardize`：`(x - mean_fit) / scale_fit`，零方差时 scale 取 1
- `minmax`：`(x - min_fit) / span_fit`，零跨度时 span 取 1

类别列支持：

- `one_hot`：按配置类别顺序展开
- `target`：固定前缀内类别标签均值，平滑公式：
  `(sum_label + prior_weight * global_mean) / (count + prior_weight)`

交叉槽位支持：

- 两类别列 `one_hot`：按类别笛卡尔积展开
- 两类别列 `target`：组合目标均值
- 两数值列 `product`：使用已变换数值的乘积

变换参数仅从排序后的前 `fit_rows` 行拟合。该规则使前缀外单行修正不会污染全局统计上下文，从而使未受影响编码键保持稳定。

## 3. 确定性选择

选择器支持：

- `variance`：总体方差 `ddof=0`
- `mutual_info`：连续特征确定分桶后与标签的互信息

互信息分桶规则：

- 唯一值不超过 `mi_bins` 时逐唯一值分桶
- 否则使用 `numpy.quantile(..., method="linear")` 生成等频边界并去重
- 常数特征分数为 0

排序规则固定为 `(-score, feature_id)`，保留前 `target_k`。分数写入报告前舍入到 12 位，保留判断与排序基于同一舍入值。

## 4. 内容寻址缓存与增量失效

编码缓存主键：

```text
sha256({
  row_id,
  feature_spec,
  source_values,
  fit_context_digest
})
```

因此：

- 某行一个字段修正时，只有引用该字段的特征/交叉键变化
- 编码方式切换时，只有新编码定义相关键变化
- 交叉槽位新增时，仅新槽位条目缺失
- 交叉槽位删除时，计划中剩余条目全部命中
- 前缀内修正会改变 `fit_context_digest`，从而安全地使全部统计依赖失效

选择快照键：

```text
sha256(row_ids, feature_ids, matrix_digest, label_digest, selector)
```

`selection_cache` 保存完整行记录与分数，并带内容校验和。`current_state.selection_key` 是唯一有效指针；任何旧键读取都返回 `E_STALE_RESULT`。缓存提交与选择指针更新在同一 SQLite 事务中完成。

## 5. 并发与资源上限

资源限制：

- `max_resident_bytes`：按行数 × 特征数估算驻留数组，超限 `E_RESOURCE_LIMIT`
- `max_cache_entries`：提交前检查合并后的唯一缓存键数
- `max_batch_rows`：`degrade_serial` 时裁至上限并记录 `batch_rows_serialized`
- `max_query_workers`：`degrade_serial` 时转串行并记录 `query_workers_serialized`

缓存写使用 `BEGIN IMMEDIATE`、`busy_timeout=0`。若另一写事务占用阶段，后到提交立即返回 `E_STAGE_BUSY`。查询进程通过只读连接访问已提交快照；WAL 模式允许查询与批次写并行。

`query_many` 使用 `ProcessPoolExecutor` 执行真实并行查询，并对请求进程数执行配置上限。降级前后响应 JSON 摘要必须一致。

## 6. 检查点协议

检查点 SQLite 表：

```text
run_meta(singleton, run_signature, status, reason, last_version)
checkpoints(version, payload_json, checksum)
```

每个批次结束时提交一次：

- `version` 严格等于前一版本 + 1
- `payload_json` 包含已提交行前缀、特征 ID、拟合上下文摘要、批号、游标
- `checksum = sha256(canonical_json(payload))`

状态语义：

- `running`：执行中；若重启时仍为 `running` 且有检查点，判定为强杀恢复
- `graceful_stop`：在批次边界主动停止，记录原因
- `completed`：全部批次已提交

恢复只读取最新且全链校验通过的检查点。检查点之前的行不重新编码；游标之后的行重新编码。任何未落盘的半批次编码或未提交选择状态都没有持久化入口，因此恢复时自然丢弃。

## 7. 快照格式与迁移

v1：

- 单文件 JSON `feature_config/v1`
- `effective_at_sec` 相对秒
- 可选 `epoch_ms` 锚点，生成样本固定为 1704067200000
- 缺失的可选字段由规范化器补全，例如 limits、resource policy、选择 tie-break、MI 分桶数

v2：

- 分片 `base.json`、`numeric.json`、`categorical.json`、`cross.json`、`selection.json`、`resources.json`
- `index.sqlite` 记录 `effective_at_ms`、版本、分片摘要、整体配置摘要、补全来源
- v2 加载时逐分片校验字节 SHA-256，再校验整体配置摘要

v1 -> v2：

- `effective_at_ms = epoch_ms + effective_at_sec * 1000`
- 目标版本必须不低于源版本，也不得低于目标位置已有版本

v2 -> v1：

- `effective_at_sec = (effective_at_ms - epoch_ms) / 1000`
- 逻辑快照版本保留，不因 schema 降级回退

迁移写入临时目录/临时文件后替换目标。任何校验、解析、回退检查失败都在写目标前返回 `E_SNAPSHOT_ROLLBACK`。

## 8. 查询与延迟

查询步骤固定：

1. `validate_request`
2. `load_current_selection`
3. `locate_row_record`
4. `decode_encoding_vector`
5. `project_retained_features`

预算检查在每一步之后执行，超时返回 `E_LATENCY_BUDGET` 与 `completed_steps`。

时钟：

- `PerfClock`：`time.perf_counter_ns()`，CLI `query` 默认模式，telemetry 使用
- `LogicalClock`：按相同执行步骤累加固定逻辑纳秒，canonical 报告使用

估算公式固定记录在每份查询响应和报告中：

```text
base_ns (25000)
+ per_row_ns (8) * row_count
+ per_feature_ns (45) * feature_count
+ per_retained_feature_ns (7) * retained_feature_count
```

## 9. 业务错误码

| 业务码 | 触发 |
| --- | --- |
| `E_PARAM_INVALID` | 配置参数非法、交叉槽位引用缺失、目标保留数越界 |
| `E_BAD_INPUT` | 重复 `row_id`、缺列、非有限数值/标签、未知查询行 |
| `E_ENCODING_ERROR` | 类别值越界、缓存条目/选择载荷不可解析 |
| `E_LATENCY_BUDGET` | 查询超出调用方注入预算 |
| `E_SNAPSHOT_ROLLBACK` | 快照回退、校验和/摘要不符、字段不可解析 |
| `E_STALE_RESULT` | 选择键不再指向当前提交 |
| `E_RESOURCE_LIMIT` | 矩阵、缓存、批次或查询进程上限 |
| `E_STAGE_BUSY` | 写缓存提交阶段冲突 |
| `E_CHECKPOINT_CORRUPT` | 检查点版本链、摘要、签名或载荷损坏 |
| `E_INTERNAL_ERROR` | 顶层未显式处理异常；记录在 CLI/诊断边界，不静默吞掉 |

## 10. 可复现报告策略

canonical 报告不包含时间戳、进程号、绝对临时路径或真实墙钟值。阶段耗时使用确定性工作单位时钟；单行查询延迟使用逻辑时钟。真实 `perf_counter_ns` 结果单独写入 `artifacts/run_telemetry.json`，并通过 `canonical_report_sha256` 关联。

这样可同时满足：

- 同样本、同配置、同种子重复运行得到逐字节相同 canonical JSON
- 本机查询/阶段真实处理时间仍有独立可观测产物
