# 实现说明

本仓库交付一个仅依赖 `numpy` 与 Python 标准库的传感器窗口异常检测库 `drift_ae`，并提供确定性夹具、完整示例、恢复验收脚本和 `unittest` 测试。

## 快速运行

```bash
pip install -r requirements.txt
python scripts/generate_deterministic_fixtures.py
python examples/end_to_end_demo.py --fresh
python -m unittest discover -s tests -v
python scripts/run_acceptance.py
```

强制终止与恢复演示：

```bash
python examples/end_to_end_demo.py --fresh \
  --state-dir artifacts/recovery-demo --crash-after 3
# 进程返回 137 / -9 后执行：
python examples/end_to_end_demo.py --state-dir artifacts/recovery-demo
python -m drift_ae replay artifacts/recovery-demo/diagnostics.jsonl
```

## 模块映射

- `drift_ae/data.py`：固定种子 `406` 生成 96 个 `32 × 4` 窗口、三条时变正态流、尖峰/趋势变点/离群块、污染标签、修正项和历史漂移事件。
- `drift_ae/preprocessing.py`：固定规范化统计量、种子与输入摘要共同决定掩码；连续掩蔽段线性补齐，输出窗口/维度级边界追踪。
- `drift_ae/autoencoder.py`：显式 `W1/b1 ... W4/b4` 矩阵，结构为 `128 -> 32 -> 8 -> 32 -> 128`；包含逐层前向、MSE 反向梯度、确定性训练、NPZ 校验和持久化。
- `drift_ae/features.py`：均值残差、最大残差、局部平滑残差、相邻掩码一致性/边界不稳定度，以及掩蔽段衰减权重 `0.25`。
- `drift_ae/thresholds.py`：确定性经验分位、分段/全局阈值行、严格策略与容差带策略。
- `drift_ae/threshold_formats.py`：v1 单文件扁平 JSON；v2 分片 JSON + SQLite；升级补全默认粒度/校验和，降级逐项扁平化并丢弃非 `effective` 层级或条件行。
- `drift_ae/drift.py`：均值/标准差和中位数/MAD 漂移统计、归因排序、受影响时间窗和模块集合。
- `drift_ae/calibration.py`：伪标签修正、漂移事件传播、受影响分段重估、未受影响分段/决策/特征产物复用。
- `drift_ae/execution_control.py`：算力估算、确定性降级选择、线程 + `flock` 非阻塞提交门；冲突返回 `E_STAGE_BUSY`。
- `drift_ae/snapshots.py`：`plan`、`threshold`、`result` 三类快照的单调版本、SHA-256 内容校验和、临时文件 + `os.replace` 原子整体替换；回退返回 `E_SNAPSHOT_ROLLBACK`。
- `drift_ae/pipeline.py`：批次处理、批次边界快照、批次追加、重启恢复、受影响决策重裁和 JSONL 诊断记录。
- `drift_ae/replay.py`：依据诊断记录中的掩码、补齐值、重构值、配置和阈值行重放特征与裁决。
- `drift_ae/storage.py`：SQLite `metadata.sqlite`，保存窗口标签、校准运行和诊断索引。
- `scripts/generate_deterministic_fixtures.py`：生成 `artifacts/fixtures/`。
- `scripts/run_acceptance.py`：自动执行测试、真实 `SIGKILL`、恢复、基线比对和回放。

## 关键规则

### 掩码与特征

- 观测维贡献权重为 `1.0`，掩蔽维贡献权重为 `0.25`。
- 加权均值为 `sum(residual * weight) / sum(weight)`。
- 平滑残差使用宽度 5 的均匀核做局部加权平均。
- 掩码边界不稳定度为边界两侧残差跳变除以 `3 × 局部残差尺度`，截断到 `[0, 1]`。
- 每条补值和边界特征均带 `window_id`、展开维度、`time_index`、`sensor_index`。

### v1/v2 迁移

v1 的 `quantiles` 是单层扁平数值对象：

- `p50/p90/p95/p99`：全局 `score` 分位。
- `segment.<segment>.<metric>.<quantile>`：分段有效阈值。

v2 SQLite 的 `quantiles` 表以 `(segment_id, tier, metric)` 为主键，`tier` 包含 `effective`、`pseudo`、`verified` 或 v2 专有层级。升级时，v1 全局阈值会复制到缺失分段并标记 `inherited_from=__global__`、补全 `granularity=window_segment` 和行/模型校验和。降级时只保留 `tier=effective` 且无条件的行；其他层级/条件按 `drop_layered_condition` 逐项记录。

### 预算公式

```text
reconstruction_comparisons = n_windows × input_dim
resident_feature_bytes ≈ n_windows ×
  (3 × input_dim × 8 + 8 × 8 + 512)
```

超预算时优先做固定等距/分层抽样子集，并强制保留本次修正窗口；若固定修正集仍超最小紧凑草图预算，则返回 `E_BUDGET_EXCEEDED`。每次裁决的原始估算、限制、选择窗口和依据均写入校准记录。

### 快照与恢复

每个批次完成后依次写批次诊断文件、完整 `diagnostics.jsonl`、`result` 快照和 `plan` 快照。快照使用同目录临时文件、`fsync` 和 `os.replace`。恢复时以 `result.processed_window_ids` 与 `committed_batches` 为准，已提交窗口只复用，不再次执行自编码器前向。

## 集成示例

```python
from drift_ae import DetectionPipeline
from drift_ae.threshold_formats import load_threshold_model

pipeline = DetectionPipeline("artifacts/state", batch_size=8)
pipeline.set_ae(...)
pipeline.set_stats(...)
pipeline.set_threshold_model(load_threshold_model("artifacts/fixtures/threshold_model_v2"))
pipeline.run_pending(windows)

pipeline.apply_calibration({
    "reason": "false_positive_fix",
    "window_updates": [{"window_id": "w0003", "corrected_label": "normal"}],
})
```
