# 实际验证记录

验证环境：

- Python 3.10.12
- NumPy 1.26.4
- pytest 8.3.3
- Linux 本地文件系统
- 未使用 Docker/容器，未下载外部数据集或模型权重

## 自动化测试

命令：

```bash
python -m pytest -q
```

结果：

```text
35 passed
```

覆盖范围：

- 采样、时间轴、帧对排序与逐字节确定性
- `E_TIME_ORDER`、`E_TIME_SPAN` 定位
- 运动编码、遮挡占位、`E_OBJECT_OVERFLOW`、`E_MASK_SHAPE`
- `E_WINDOW_INVALID`、`E_DECAY_INVALID`、历史窗口确定性
- 缺失观测/运动突变归因与 `E_ATTRIBUTION_FAIL`
- 指标从逐帧明细独立重算
- 检查点版本、校验和损坏检测
- 三类快照整包替换、`E_SNAPSHOT_ROLLBACK`、`E_SNAPSHOT_CORRUPT`
- 优雅暂停、检查点前缀恢复、输出自检、资源限制与降级
- CLI 生成/运行/验证
- POSIX 子进程真实 `SIGKILL` 后恢复

## 端到端演示

命令：

```bash
python examples/demo.py
```

本次实际输出摘要：

```json
{
  "attribution_categories": [
    "motion_abrupt",
    "observation_missing"
  ],
  "graceful_resumed_checksum": "16cd93a52322639dc8fc393fb0e930b6af2a4e0916ced15e945c9557ce1c93d0",
  "graceful_status": "paused",
  "injection_error_code": "E_TIME_ORDER",
  "normal_predictions_checksum": "16cd93a52322639dc8fc393fb0e930b6af2a4e0916ced15e945c9557ce1c93d0",
  "resume_stage_reused": [
    "sampling",
    "encoding"
  ],
  "resumed_predictions_checksum": "16cd93a52322639dc8fc393fb0e930b6af2a4e0916ced15e945c9557ce1c93d0",
  "status": "ok"
}
```

结论：

- 正常运行的逐帧预测校验和为 `16cd93a5...c93d0`。
- 时间戳倒挂在帧 7 被定位，业务码为 `E_TIME_ORDER`。
- 缺失观测注入定位到帧 6；运动突变注入在帧 12 被定位，恢复观测时帧 13 记录二次失配。
- 强制终止发生在聚合阶段，未生成退出报告。
- 重启复用已提交 `sampling`、`encoding` 检查点，从 `aggregation` 起点重建。
- 恢复后预测校验和与不间断基线完全相同。
- 聚合阶段第 8 帧的优雅停止返回 `S_STOPPED`，仅保留采样/编码检查点；恢复后预测仍与基线一致。

## 正常报告指标

演示中 `clip_uniform` 的指标：

```json
{
  "displacement_error_px": 0.053505,
  "trajectory_hit_rate": 1.0,
  "temporal_consistency": 1.0
}
```

检查点版本：

```text
sampling=1, encoding=2, aggregation=3, output=4
```

快照类别与版本：

```text
model_parameters=v1
temporal_policy=v1
history_state=v4
```

## 注入记录示例

时间戳倒挂：

```json
{
  "stage": "sampling",
  "kind": "time_order",
  "business_code": "E_TIME_ORDER",
  "frame_index": 7
}
```

运动突变归因：

```json
{
  "frame_index": 12,
  "category": "motion_abrupt",
  "affected_targets": [0],
  "deviation": {
    "kind": "acceleration_px_per_s2",
    "value": 1392.500022,
    "threshold": 800.0
  }
}
```

## 复现说明

`examples/demo.py` 每次会只清理并重建自己的 `demo_output/` 目录。样本由固定种子 `424242` 生成，报告采用稳定 JSON 编码。正常、恢复和优雅恢复三次运行的 `predictions_checksum` 一致。

