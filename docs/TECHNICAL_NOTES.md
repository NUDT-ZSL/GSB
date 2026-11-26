# 量化核函数自动调优引擎技术说明

## 1. 范围与模块

本仓库在无 Docker、无外部服务的前提下实现：

- v1 单文件配置与 v2 “JSON 分片 + SQLite 元数据库”读取、校验和双向迁移。
- 固定种子 workload、确定性定点量化/反量化和误差摘要。
- 平台代价表、MAC/显存/延迟估算、确定性排序和本地 `ProcessPoolExecutor` 调度。
- 并行度、内存、逻辑时钟、量化精度的显式裁决与稳定错误码。
- 真实 `time.perf_counter_ns()` 循环计时、versioned ledger snapshot 和 SHA-256 校验和。
- 配置代数/quant/schedule revision 驱动的失效传播与 quant summary 缓存复用。
- `SIGKILL` 后从最近 checkpoint 恢复，丢弃未提交 dispatch/inflight/staging。
- 非阻塞提交锁和跨模块一致性自检。

主要模块：

- `quant_tuner/config_io.py`：v1/v2/unified 配置和迁移。
- `quant_tuner/workload.py`：确定性输入、量化公式和误差复算。
- `quant_tuner/estimator.py`：平台统计表和代价模型。
- `quant_tuner/planner.py`：排序、预算/精度联合裁决、批次计划。
- `quant_tuner/worker.py`：进程池 worker、tiled kernel 和实际计时。
- `quant_tuner/ledger.py`：原子快照、校验和、`E_STAGE_BUSY` 非阻塞锁。
- `quant_tuner/recovery.py`：checkpoint 恢复和未提交文件清理。
- `quant_tuner/propagation.py`：失效传播和摘要缓存。
- `quant_tuner/self_check.py`：账本、批次、误差和集合一致性检查。
- `scripts/run_acceptance.py`：真实 `SIGKILL` 验收。

## 2. 统一配置模型

两代配置都迁移为下列记录：

```json
{
  "record_id": "cfg-mm-0001-mm-t4-wide",
  "tuning_id": "mm-0001",
  "absolute_ms": 1700000005251,
  "generation": 1,
  "quant_config": {"bits": 8, "scale": 0.035, "zero_point": 0},
  "schedule": {"schedule_id": "mm-t4-wide", "threads": 4}
}
```

- v1：`origin_absolute_ms + effective_second * 1000` 得到 `absolute_ms`；`quant_scale`、`schedule_threads` 等扁平字段映射到嵌套结构。
- v2：`shards/shard-*.json` 保存记录，`meta.sqlite3:config_meta` 保存绝对毫秒、代数、shard 路径和摘要；记录摘要与 SQLite 摘要必须一致。
- 未知 `version` 抛出 `E_VERSION_UNSUPPORTED`。
- 缺字段、JSON/SQLite 损坏、摘要不符、同一 `tuning_id` 时间戳倒退均抛出 `E_FORMAT_CORRUPT`。

## 3. 确定性定点量化

显式公式和舍入模式为：

```text
q = clip(round_half_away_from_zero(x / scale) + zero_point, qmin, qmax)
x_hat = (q - zero_point) * scale
```

- 舍入：tie 向远离零方向，不用 NumPy 默认 banker's rounding。
- 边界：显式 `qmin/qmax`、`dtype=uint8` 时用 `[0,2^bits-1]`；否则默认有符号 `[-2^(bits-1),2^(bits-1)-1]`。未声明 dtype 且 `zero_point >= 2^(bits-1)` 时按无符号推断。
- workload 种子来自 `SHA256(tuning_id|op_kind|canonical_shape|role)`，不使用 Python 内置 `hash()`。
- 指标包括 `mae`、`rmse`、`max_abs`、`relative_rmse`、裁剪数和量化数组字节摘要；指标摘要为规范化 JSON 的 SHA-256。

因此相同 `tuning_id + workload_shape + quant_config + effective_bits` 在同一平台/NumPy 主版本上产出逐字节一致的指标文档。

## 4. 调度与代价

任务排序键：

```text
(priority 升序, logical_clock_ms 升序, tuning_id)
```

候选排序键：

```text
(schedule_id, schedule_revision, canonical_schedule_digest)
```

点 ID 由 tuning、schedule 内容和 quant digest 共同派生。计划按选定并发度切成连续批次；worker 结果用 `executor.map` 按请求顺序归并，不按完成顺序归并。

估算输出包含：

- `mac_budget` / `macs`
- `dequant_ops`
- `resident_memory_mb`
- `expected_latency_us`
- cache spill、memory/compute/dequant 分项和核间干扰系数

实际计时在 worker 内执行 warmup 后用 `time.perf_counter_ns()` 采样多轮并取中位数，记录 `samples_ns`、`elapsed_ns_median` 和 `latency_us`。

## 5. 并行、内存与精度裁决

策略文件的 `arbitration.priority_order` 必须是 `progress`、`memory`、`precision` 的排列。引擎枚举：

- `concurrency = 1..min(max_concurrency, worker_count)`
- `precision_mode = sla | max`

每个选项计算准入点数、并发度、估算 logical makespan、峰值显存、峰值干扰和平均量化位宽，然后按声明的优先级构造字典序排名：

- `progress`：准入点数、并发度、`-makespan`
- `memory`：`-peak_memory_mb`、`-peak_interference`
- `precision`：平均位宽、`-relative_rmse`

硬限制包括显存、反量化操作量、干扰系数和 logical clock 预算。无法满足时：

- 精度 SLA 即使升到 `high_precision_bits` 仍不满足：`E_PRECISION_TRADE`。
- 精度合规但超出显存/计算/目标延迟/logical clock：`E_BUDGET_EXCEEDED`。

## 6. 快照、并发提交和恢复

输出目录：

```text
checkpoints/checkpoint-000001.json   # 权威提交点
checkpoints/latest.json
ledger/current.json
ledger/history/ledger-000001.json
work/batch-000002.dispatch.json      # 未提交时会在恢复时删除
work/batch-000002.inflight
staging/<point>.partial.json
```

快照 payload 包含结果、已提交批次、`next_batch_no`、输入摘要和 logical clock。外层记录：

```json
{"schema_version": "quant-tuner-ledger/v1", "snapshot_version": 4, "checksum": "sha256:...", "payload": {}}
```

校验和是 `canonical_json(payload)` 的 SHA-256。提交时用 `fcntl.flock(LOCK_EX | LOCK_NB)` 抢 `ledger/commit.lock`；抢锁失败立即返回 `E_STAGE_BUSY`，不等待。提交采用临时文件、fsync、`os.replace`，版本号在锁内从磁盘最大版本单调加一。

恢复规则：

1. 读取最大的 `checkpoint-N.json` 并验证 schema/checksum。
2. 删除所有 `*.inflight`、`*.dispatch.json`、`staging/*.partial.json` 和原子写临时文件。
3. 从 `payload.next_batch_no` 继续。
4. 已在快照中的 point 不重算；batch 1 的实测计时在 SIGKILL 恢复验收中逐字节保持。
5. 完整退出报告只在所有准入批次提交后生成；自检保证报告中的 point 集合是账本集合子集。

`SnapshotStore.refresh_if_changed()` 发现磁盘版本或 checksum 变化时会重新读取完整 JSON 并整体替换内存快照，不做 JSON patch。

## 7. 失效传播

传播图包含：

- `quant:<tuning_id>:<quant_digest>`：可复用上游量化摘要。
- `point:<point_id>`：候选调度评估点。

规则：

- quant config 变化或 v2 代数提升带来 quant 变化：该 tuning 的 quant summary 和全部候选 point 重算。
- 仅 schedule revision/内容变化：该 point 重算，上游 quant summary 复用。
- 无变化：point 与 summary 均复用。

输出同时给出：

- `recompute_set` / `reuse_set` / `affected_set`：受影响闭包必须构成无交集划分。
- `full_recompute_set` / `full_reuse_set` / `coverage_universe`：全量节点划分。
- `recompute_points` / `reuse_points` / `recompute_quant_summaries` / `reuse_quant_summaries`。

`quant-cache.json` 以 quant digest 为键；验收先播种旧摘要，修订后只有变化的 mm summary 重算，另外三个未变 summary 实际复用。

## 8. 自检错误码

`python -m quant_tuner.cli verify ...` 校验：

- 所有 checkpoint checksum 和连续版本号。
- 输入摘要一致。
- 每个账本 point 都在确定性计划中，sequence/batch 一致。
- 量化指标可独立重算且 digest 相同。
- 已提交批次中的 point 集合与计划批次完全相等。
- 不存在遗留 inflight marker。
- run report 不包含未提交 point。
- 传播 recompute/reuse 集合无交集且覆盖 affected/universe。

失败返回 `E_INCONSISTENT`。

## 9. 常用命令

```bash
python3 -m pip install -r requirements.txt
python3 -m quant_tuner.cli generate-samples --output samples
python3 -m unittest discover -s tests -v
./scripts/acceptance.sh
```

手动运行与恢复：

```bash
python3 -m quant_tuner.cli run \
  --tasks samples/tuning_tasks.jsonl \
  --platform samples/platform_stats.json \
  --config samples/engine_config.json \
  --output runs/demo

python3 -m quant_tuner.cli run --resume \
  --tasks samples/tuning_tasks.jsonl \
  --platform samples/platform_stats.json \
  --config samples/engine_config.json \
  --output runs/demo

python3 -m quant_tuner.cli verify \
  --tasks samples/tuning_tasks.jsonl \
  --platform samples/platform_stats.json \
  --config samples/engine_config.json \
  --output runs/demo
```
