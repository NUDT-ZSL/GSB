# 实际验证记录

验证日期：2026-11-23  
运行环境：Python 3.10.12，NumPy 1.26.4，本地文件系统。代码仅使用标准库与 NumPy，路径与原子替换实现兼容 Windows。

## 1. 自动化测试

命令：

```bash
python -m unittest discover -v
```

结果：

```text
Ran 15 tests in 7.349s
OK
```

覆盖内容：

- 窗口、特征矩阵和目标向量重复生成一致，训练/测试时间隔离。
- 分块训练重复运行模型参数摘要和逐轮摘要一致，并逐轮对照整段训练。
- 线性 AR 和树桩梯度提升两种模型族。
- float64、float32、定点三档共享窗口与特征布局；定点整数编码可独立重放。
- 递归预测的真实目标访问上界小于预测目标索引。
- 非法递归深度返回 `E_RECURSION_INVALID` 并包含窗口索引与步号。
- 误差谱由逐窗口偏差重算；非法区间/级别返回 `E_ERROR_SPECTRUM_INVALID`。
- 指标、预测和误差谱篡改可被 `E_INCONSISTENT` 定位。
- 门限附近在容差带策略下跨精度档判定一致。
- 非法门限/容差返回 `E_CONFIG_INVALID` 及精确字段。
- 快照整体替换、旧版本回滚、校验和损坏、重启同步及策略历史。
- CLI 正常报告与业务错误 JSON。

## 2. 确定性样本

命令：

```bash
python scripts/generate_sample.py \
  --config configs/sample_config.json \
  --output data/sample_series.json
```

结果：

```text
generated length=320 seed=20261123
target_sha256=12b9ca4ed2b9609206944d17eb8e4eee634ef1119e5ad14699df8ee9b32bff42
```

保存后重新加载，目标数组摘要和内容校验和完全一致。

## 3. 主样例报告

命令：

```bash
python main.py \
  --config configs/sample_config.json \
  --series data/sample_series.json \
  --output reports/sample_report.json \
  --print-summary
```

摘要：

```json
{
  "business_code": "OK",
  "consistency": "OK",
  "deterministic_core_sha256": "c5aead29910fa9bb399060c8f3430b9f1486569f6206d024db12ef401eeea4a9",
  "dominant_error_source": "recursive_prediction",
  "metrics": {
    "mae": 0.9905198185307839,
    "rmse": 1.3191759783446217
  },
  "model_family": "linear_autoregressive",
  "precision_profile": "float64",
  "snapshots": {
    "error_spectrum": 1,
    "precision_config": 1,
    "window_stats": 1
  },
  "window_counts": {
    "test": 71,
    "train": 211
  }
}
```

误差谱：

| 级别 | 绝对误差 | 区间 |
|---|---:|---:|
| `window_boundary` | 0.0 | [0.0, 0.0] |
| `feature_scaling` | 0.0 | [0.0, 0.0] |
| `chunk_gradient_accumulation` | 2.7069388328840115e-15 | 非负 |
| `fixed_point_quantization` | 0.0 | [0.0, 0.0] |
| `recursive_prediction` | 0.992578232562876 | 非负 |

门限通过率：`0.4471830985915493`。受保护判决：11 个。

## 4. 三档精度一致性

同一配置分别运行 float32 与定点档：

| 档位 | MAE | RMSE | 主导来源 | 总误差 vs float64 |
|---|---:|---:|---|---:|
| float64 | 0.9905198185 | 1.3191759783 | `recursive_prediction` | 2.707e-15 |
| float32 | 0.9905196556 | 1.3191757923 | `recursive_prediction` | 2.875e-07 |
| fixed_point | 0.9905130112 | 1.3191683676 | `recursive_prediction` | 1.155e-05 |

三档的：

```text
window_collection_digest = e8f7817977ae7c0b...
first_window_feature_digest = 376f4782c10ea448...
train/test window counts = 211 / 71
```

全部相同，说明切换精度档未改变窗口集合、特征列顺序和切分边界。定点审计中每条记录均满足：

```text
replayed == integer_code / fixed_scale
```

## 5. 报告逐字节复现

在相同输入、相同配置、相同快照目录下重复运行主样例，输出文件 SHA-256：

```text
30ca9f0d419c30e7c66f4fcbb426aa7fed38419e4b1d4b2e2ce2d07884d1f146  reports/sample_report.json
30ca9f0d419c30e7c66f4fcbb426aa7fed38419e4b1d4b2e2ce2d07884d1f146  /tmp/.../report.json
REPORT_BYTE_IDENTICAL_WITH_SNAPSHOTS
```

相同内容重复运行不会提升快照版本；三个快照都保持版本 1。

离线复核：

```bash
python scripts/verify_report.py --report reports/sample_report.json
```

结果：

```text
status = OK
checks = window_id_sets, prediction_recompute, metrics_recompute,
         spectrum_recompute, threshold_recompute
checked_steps = 284
protected_decisions = 11
skipped_decisions = 0
```

## 6. 快照回滚与损坏

临时快照场景中，先以容差带策略提交，再切换到严格策略：

```text
error_spectrum 1
precision_config 2
window_stats 1
```

随后指定旧版本 1：

```text
exit = 2
E_SNAPSHOT_ROLLBACK
snapshot_name = precision_config
version = 1
message = cannot replace snapshot 'precision_config' version 2 with older version 1
```

手工修改 `precision_config` 的 payload 但不更新校验和：

```text
exit = 2
E_SNAPSHOT_CORRUPT
snapshot_name = precision_config
version = 2
```

## 7. 其他业务码

非法递归深度：

```text
exit = 2
E_RECURSION_INVALID
window_index = 112
window_id = w000126
step = 4
```

非法门限：

```text
exit = 2
E_CONFIG_INVALID
field = evaluation.threshold
```

自动化测试还直接验证了：

- 区间上界小于下界或级别不在受控集合：`E_ERROR_SPECTRUM_INVALID`。
- 篡改预测、指标或误差谱：`E_INCONSISTENT`，并返回首个偏差位置。

## 8. 结论

本地样例已覆盖验收要求：分块训练逐轮可复现、三档精度共享同一阶段语义、逐级误差谱与主导归因可由明细重算、递归预测不读取未来目标、门限判决稳定、快照整体替换/回滚/损坏检测有效，CLI 报告包含全部业务字段和一致性结论。
