# 确定性视频预测与运动时序管线

这是一个从零构建的本地数据处理与结果验证项目，面向“帧采样 → 运动编码 → 递归时序外推 → 结果汇总”四阶段视频预测流程。项目只使用 Python 标准库与 NumPy，不访问外部服务或数据集。

核心保证：

- 固定种子、固定配置下，样本、帧对、轨迹特征、预测和最终结果逐字节可复现。
- 阶段只有在全部 `clip_id` 完成后才允许提交，提交使用临时文件 `fsync` 后 `os.replace` 原子替换。
- 强制终止后从最后一个完整阶段快照恢复；已提交阶段复用，未提交阶段的中间状态丢弃并从阶段起点重建。
- 恢复运行与不间断基线的最终预测、统计、检查点版本序列和最终结果字节一致。
- 快照损坏、校验和不符、版本回退、并发提交冲突均有独立业务码。

完整验证结果见 [`docs/VERIFICATION.md`](docs/VERIFICATION.md)，机器生成的演示报告为 `demo-output/report.json`。

## 环境与快速开始

要求 Python 3.10+ 和 NumPy。当前验证环境为 Python 3.10.12、NumPy 1.26.4。

```bash
# 1. 生成固定种子的本地样本
python3 -m motion_pipeline generate --out data/sample --seed 20261123 --clips 6

# 2. 完整基线运行
python3 -m motion_pipeline run \
  --manifest data/sample/manifest.jsonl \
  --frames data/sample/frames.jsonl \
  --workdir run \
  --reset \
  --report run/reports/run.json

# 3. 自检
python3 -m motion_pipeline verify --workdir run --report run/reports/verify.json

# 4. 完整演示：基线 + 真实强制终止 + 恢复 + 异常/快照防护探针
python3 -m motion_pipeline demo --out demo-output

# 5. 测试
python3 -m unittest discover -s tests -v
```

`run` 默认在已有检查点时恢复。`--reset` 只清理本项目的快照、版本账本和最终结果，不删除样本文档。

## 项目结构

```text
motion_pipeline/
  config.py         配置、资源限制、受控枚举
  dataset.py        确定性样本生成、JSONL 加载与输入校验
  deterministic.py  规范 JSON、SHA-256、原子文件写入
  stages.py         四个确定性阶段
  snapshots.py      四阶段状态机、检查点、锁、版本账本
  pipeline.py       阶段编排、恢复复用、最终结果
  verification.py   跨阶段一致性、指标重算、恢复比较
  cli.py            generate/run/verify/compare/demo
  __main__.py       python -m motion_pipeline 入口
tests/
  test_generation_validation.py
  test_stages.py
  test_recovery.py
  test_snapshots.py
  test_resources_and_cli.py
docs/VERIFICATION.md
```

## 确定性样本

`generate` 生成：

- `manifest.jsonl`：每行严格包含 `clip_id`, `frame_count`, `frame_interval_ms`, `object_count`, `motion_mode`, `seed`, `temporal_policy`。
- `frames.jsonl`：每个 clip 的逐帧稀疏运动掩码、稀疏像素和目标状态。画布固定为 32×32。
- `dataset_meta.json`：生成器版本、固定种子、帧尺寸以及 manifest/frames 的 SHA-256。

生成过程没有系统时间、进程随机源或全局 RNG。每个对象使用由 `clip seed + clip_id + object_count + object_id` 派生的 NumPy `PCG64` 生成器。受控运动模式为：

- `constant`：匀速轨迹。
- `accelerated`：匀加速轨迹。
- `periodic`：正弦周期轨迹。

受控采样策略为 `every_frame`、`stride_2`、`stride_3`。

## 四阶段语义

### 1. `frame_sample`

对每个序列按 `temporal_policy` 的步长在观测前缀内展开帧对。观测帧数是：

```text
obs = max(2, min(frame_count - 1, round(frame_count * observation_ratio)))
```

因此至少保留一个外推帧：

- 观测帧集合为 `[0, obs)`。
- 外推帧集合为 `[obs, frame_count)`。
- 两者不相交，并集严格覆盖全部帧。

帧对携带 `from_frame_index`, `to_frame_index`, `step_frames`, `step_ms`, `step_s`。帧对按 `(from_frame_index, to_frame_index, step_frames)` 升序排列；相同间隔或同分时的稳定规则是 `frame_index_ascending`。每个阶段记录包含 `sample_checksum`。

### 2. `motion_encoding`

对每对帧，用稀疏掩码确定对象支撑，再用对应稀疏像素强度加权计算对象质心。每个样本包含：

- `displacement`：帧对质心位移。
- `velocity`：按相对时间步得到的像素/秒速度。
- `speed`：速度模长。
- `acceleration`：与前一样本速度差分得到的像素/秒²加速度；首样本为零。
- `confidence`：掩码支撑覆盖率和帧间隔惩罚共同得到的置信度。

对象顺序和帧对顺序均为确定性的，每个 clip 记录 `feature_checksum`。

### 3. `temporal_extrapolation`

从最后一个观测帧开始逐帧递归外推。每个对象维护长度不超过 `history_window_length` 的历史窗口，通过 `decay` 加权最小二乘估计局部速度，再结合衰减后的加速度预测下一中心、框和 3×3 稀疏掩码。

每个预测帧显式携带：

```json
{
  "state": {
    "input_history_summary_hash": "...",
    "previous_consumed_frame_index": 12,
    "output_history_summary_hash": "...",
    "consumed_frame_index": 13,
    "history_window_length": 6
  }
}
```

clip 末尾还记录最终历史摘要哈希和下一帧索引。窗口长度超出 `[1, max_history_window_length]` 返回 `E_WINDOW_INVALID`；`decay` 非有限或非正返回 `E_DECAY_INVALID`；两者均指明首个受影响 `clip_id`。

### 4. `result_aggregation`

比较预测与真实未来帧，逐帧逐对象记录：

- 中心误差、框 IoU、掩码 IoU、预测置信度、命中布尔值。
- 命中条件为中心误差不超过 `hit_distance_px`。
- 每序列统计和整体统计均可由逐帧明细独立重算。

`verify` 会重新计算每个 clip 与整体指标并逐值比对，同时验证四阶段的 `clip_id` 集合、观测/外推帧覆盖、预测帧索引、历史状态、内容校验和和最终结果。

## 快照与状态机

运行时目录：

```text
<workdir>/
  snapshots/
    checkpoint.json
    committed_ledger.json
    .commit.lock
  final/result.json
  reports/
```

每个检查点是完整快照，而不是增量补丁。快照包含：

- 单调递增全局 `version`，正常流程为 1、2、3、4。
- `committed_stage`、`committed_stage_index` 和下一个阶段 `cursor`。
- 所有阶段完整 `completed_clip_ids`。
- 每阶段 `content_checksum` 与完整 payload。
- 内嵌 `version_history`。
- 覆盖整个快照的 `snapshot_checksum`。

提交规则：

1. 阶段只能按 0→1→2→3 顺序提交。
2. 必须提供全部任务 `clip_id`；少一个即 `E_PARTIAL_COMMIT`。
3. 使用 `flock(LOCK_EX|LOCK_NB)` 获取提交锁，拿不到立即返回 `E_STAGE_BUSY`。
4. 生成完整新快照，写临时文件、`fsync`、`os.replace`、`fsync` 目录。
5. 再原子更新 `committed_ledger.json` 高水位版本与检查点校验和。

读取规则：

- JSON 不完整、阶段 payload 校验和不符、顶层校验和不符均返回 `E_SNAPSHOT_CORRUPT`，并尽可能给出阶段名与版本号；损坏数据绝不参与推进。
- 如果版本账本高于磁盘快照版本，说明旧快照覆盖了新版本，返回 `E_SNAPSHOT_ROLLBACK`。
- 检查点新于账本时视为“快照已落盘但账本尚未更新”的合法崩溃窗口，从完整检查点修复账本。
- `replace_snapshot` 只允许同版本显式整体替换或更高版本替换；更低版本一律 `E_SNAPSHOT_ROLLBACK`。

恢复规则：

- 读取最后完整阶段，复用其 payload，不重算该阶段任何 clip 或帧。
- 从下一个未提交阶段第一个 clip 开始。
- 未提交阶段的递归窗口、预测帧和部分统计全部丢弃。
- 恢复报告明确列出 `reused` 与 `recomputed` 阶段及 clip 集合。
- `compare` 或 `demo` 发现首个分歧阶段时返回 `E_RECOVERY_MISMATCH`。

## 资源限制与降级

可配置上限：

- `max_frames_per_clip`：单序列帧数，超限 `E_RESOURCE_LIMIT`。
- `max_resident_history_windows`：驻留历史窗口上限。超过时按策略报错，或标记 `history_window_batched` 并保持产物不变。
- `max_parallel_clips`：并行序列数上限。超过时按策略报错，或标记 `parallel_sequential`，实际以单序列顺序执行。
- `max_history_window_length`：窗口长度业务上限，超限 `E_WINDOW_INVALID`。

降级标记只存在于报告，不进入任何已提交阶段 payload 或最终结果，因此不会改变预测、统计或检查点字节。

## 退出与强制终止

- `SIGINT` / `SIGTERM`：设置停止标记，当前阶段完成后原子提交，在下一阶段边界停止并写 `exit.json`，退出码 130。
- `--kill-after-stage N`：在指定阶段提交后立即 `os._exit(137)`，不写退出报告，用于真实强制终止演示。
- `SIGKILL` 或任意更早的进程死亡：只承诺基于最后完整检查点恢复，不承诺退出报告。

## 业务码

| 业务码 | 含义 | 主要归因字段 |
|---|---|---|
| `E_DUPLICATE_CLIP_ID` | `clip_id` 重复 | `field=clip_id`, `clip_id` |
| `E_FRAME_COUNT_MISMATCH` | manifest 帧数与帧记录数不符 | `field=frame_count`, `clip_id` |
| `E_MASK_SHAPE_MISMATCH` | 掩码/画布尺寸或坐标非法 | `field=mask`, `clip_id` |
| `E_TEMPORAL_POLICY_INVALID` | 未知采样策略 | `field=temporal_policy`, `clip_id` |
| `E_FRAME_INTERVAL_INVALID` | 帧间隔非正 | `field=frame_interval_ms`, `clip_id` |
| `E_MOTION_MODE_INVALID` | 运动模式不在受控集合 | `field=motion_mode`, `clip_id` |
| `E_WINDOW_INVALID` | 历史窗口越界 | `field=history_window_length`, `clip_id` |
| `E_DECAY_INVALID` | 衰减系数非有限或非正 | `field=decay`, `clip_id` |
| `E_PARTIAL_COMMIT` | 阶段未覆盖全部 clip、乱序或跳阶段 | `stage`, `field=completed_clip_ids` |
| `E_STAGE_BUSY` | 并发提交冲突，立即失败不等待 | `stage` |
| `E_RESOURCE_LIMIT` | 配置资源上限被超过 | `field`, `clip_id`, `limit` |
| `E_SNAPSHOT_CORRUPT` | 快照损坏或校验和不符 | `stage`, `version` |
| `E_SNAPSHOT_ROLLBACK` | 旧版本试图覆盖新版本 | `stage`, `version` |
| `E_RECOVERY_MISMATCH` | 恢复结果或检查点序列与基线分歧 | `stage`, `version` |
| `E_CROSS_STAGE_INVALID` | 跨阶段一致性或独立重算失败 | `stage`, `clip_id`, `field=check` |

其他输入结构错误使用 `E_MANIFEST_INVALID`、`E_FRAME_DATA_INVALID`、`E_OBJECT_STATE_INVALID`、`E_CONFIG_INVALID`。

## CLI 与报告

### `run`

关键参数：

```text
--kill-after-stage {1,2,3,4}
--inject-fault {duplicate_clip_id,frame_count_mismatch,
                unknown_temporal_policy,frame_interval_nonpositive,
                motion_mode_unknown,resource_limit,window_invalid,decay_invalid}
--history-window, --decay, --observation-ratio
--max-frames-per-clip, --max-resident-history-windows
--max-parallel-clips, --max-history-window-length
--resource-policy {degrade,error}
--hit-distance-px
```

成功报告字段包括：

- `stage_versions`：阶段名、版本、内容校验和、完成 clip 集合。
- `reused` / `recomputed`：恢复复用和本次重算的阶段与 clip。
- `degradations` 与 `degradations_applied`。
- `checkpoint_sha256`、`final_result_sha256`。
- `cross_stage_verification`。
- `overall_metrics`、`per_clip_summary`。

失败报告包含顶层 `business_code` 和完整 `business_error`。

### `verify`

验证完整检查点、四阶段集合一致性、观测/外推覆盖、历史状态、指标重算和最终结果。

### `compare`

比较基线与恢复运行的阶段 payload、版本序列和最终结果字节；首个差异返回 `E_RECOVERY_MISMATCH`。

### `demo`

自动执行：

1. 固定种子生成样本。
2. 完整基线。
3. 独立子进程在指定阶段提交后 `os._exit(137)`。
4. 重启恢复并逐字节比较。
5. 重复 `clip_id` 异常注入。
6. 快照损坏、旧版本回滚探针。
7. 同版本整快照原子替换探针。

## 设计边界

- 固定画布 32×32；manifest 只描述任务，原始帧数据在 `frames.jsonl`。
- 正常流程假设单一活跃流水线提交者；并发提交由非阻塞锁拒绝后到者。
- `max_parallel_clips` 降级类别表示计划并行度超限后顺序执行，不产生并行调度差异。
- 字节一致性承诺针对相同 Python/NumPy 运行环境；项目不依赖系统时间、UUID 或全局随机状态。
