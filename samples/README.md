# 固定样本说明

所有文件由 `scripts/generate_samples.py` 生成，JSON 使用排序键和固定两空格缩进，不包含生成时间或随机值。

## 函数清单

`functions.json`：

```json
{
  "global_concurrency_budget": 4,
  "functions": [
    {
      "name": "resize-image",
      "runtime": "python3.11",
      "layer_refs": ["base-runtime", "image-libs"],
      "layer_params": {
        "base-runtime": {
          "env": {"PYTHONHASHSEED": "0"},
          "init_logical_ms": 9
        },
        "image-libs": {
          "env": {"OMP_NUM_THREADS": "1", "PILLOW_SIMD": "1"},
          "init_logical_ms": 21
        }
      },
      "memory_mib": 512,
      "max_concurrency": 2,
      "warm_pool_target": 2,
      "warm_ttl_ms": 2000,
      "init_base_ms": 15
    }
  ]
}
```

`layer_params` 必须与 `layer_refs` 一一对应。有效层集合包含直接引用和传递依赖。

## 层清单

`layers.json` 中 `depends_on` 构成层 DAG；`referenced_by` 必须等于函数清单反向引用的精确集合。版本必须是正整数，补丁只能增加版本。

## v1 快照

`snapshots/v1.json` 的 `clock` 声明换算语义：

```json
{
  "base_ms": 0,
  "scale_num": 5,
  "scale_den": 4,
  "rounding": "half_up"
}
```

换算公式：

```text
absolute_init_ms = base_ms + round_half_up(relative_init_ms * scale_num / scale_den)
```

迁移记录保存原始相对值、比例、舍入方向、绝对值和 `rounding_error_bound_ms=0.5`。`warm_hit` 原样保留，迁移不会推断新的预热状态。

## v2 快照

`snapshots/v2/index.json` 指向 3 个分片和 `v2_meta.sqlite`。分片提供 `absolute_init_ms`；SQLite 表 `snapshot_metadata` 提供：

- `logical_clock_ms`
- `warm_generation`
- `hit_watermark`
- `shard_id`

解析时要求分片的函数、逻辑时钟、预热代数和命中标记与 SQLite 元数据完全一致。合并时按 `(logical_clock_ms, warm_generation, source_rank)` 选择最新观测；`source_rank(v2) > source_rank(v1)`。样本中 `report-csv` 只在 v1 出现，其余函数选择 v2。

## 调用轨迹

`traces/before.json` 覆盖预热复用、函数预算放弃、全局预算放弃和冷启动成功。`traces/after.json` 覆盖层修改后的冷启动、墓碑拒绝和未受影响实例复用。

`traces/init-deadline.json` 的等待预算为 2 ms，小于确定性初始化耗时，用于验证启动后在截止时刻主动中止。

`traces/active-cancel.json` 使用 `cancel_after_ms=10`，用于验证 `ABANDONED_ACTIVE`。

`traces/crash-report.json` 和 `traces/crash-report-after.json` 用于强制终止与恢复。

## 补丁

- `patches/layer-update.json`：修改 `image-libs`、墓碑 `legacy-layer`、新增 `cold-cache`。受影响函数为 `legacy-handler` 和 `resize-image`；`classify-text` 与 `report-csv` 不受影响。
- `patches/runtime-update.json`：修改 `base-runtime`，用于让恢复场景中正在初始化的 `report-csv` 继续按旧层版本完成，完成后失效。

## 固定逻辑结果

| 场景 | 关键结果 |
| --- | --- |
| before seq 1/2 | `REUSED_WARM` |
| before seq 3 | `BUDGET_NOT_STARTED` / `E_FUNCTION_CONCURRENCY` |
| before seq 5 | `BUDGET_NOT_STARTED` / `E_GLOBAL_BUDGET` |
| before seq 6 | `COLD_START_SUCCEEDED` |
| after seq 11/12 | 未受影响实例 `REUSED_WARM` |
| after seq 13 | 修改层绑定后 `COLD_START_SUCCEEDED` |
| after seq 14 | `FAILED_PRE_START` / `E_LAYER_TOMBSTONED` |
| init-deadline | `FAILED_AFTER_START` / `E_INIT_DEADLINE` |
| active-cancel | `ABANDONED_ACTIVE` / `E_ACTIVE_CANCEL` |
| crash + resume | 旧绑定调用完成，实例转 `INVALIDATED`；新调用重新冷启动 |
