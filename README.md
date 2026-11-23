# gbtdiag — 表格回归梯度提升树诊断组件

面向本地确定性表格样本的梯度提升树集成组件，聚焦训练/推理链路的
**可复现验证**与异常注入下的**归因诊断**。仅依赖 `numpy==1.26.4`，
无 GPU、无联网、无外部数据文件。

## 四个对外入口

| 入口 | 作用 |
| --- | --- |
| `gbtdiag.train` | 确定性逐轮训练，输出逐轮逐树明细 |
| `gbtdiag.predict` | 基于存储树贡献的推理 |
| `gbtdiag.inject_diagnose` | 四阶段异常注入、降级与归因 |
| `gbtdiag.validate` | 双训哈希、独立复算、容差与归因追溯 |

## 快速开始

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v
python examples/run_demo.py
```

最小示例：

```python
from dataclasses import replace
import gbtdiag as g

config = g.TrainConfig(n_trees=20, max_depth=3, seed=20261124)
dataset = g.make_regression_dataset(n_rows=512, seed=20261124)

model = g.train(dataset, config)
prediction = g.predict(model, dataset.X)

report = g.validate(dataset, replace(config, precision="float32"))
print(report["verification"]["passed"])

specs = [
    g.make_injection("category_oor", feature_index=5, sample_indices=[1, 2]),
    g.make_injection("duplicate_threshold", round_index=2),
    g.make_injection("nonfinite_hessian", round_index=3, value=float("nan")),
    g.make_injection("negative_learning_rate", round_index=4, value=-0.1),
]
diagnostic = g.inject_diagnose(dataset, config, specs)
print(diagnostic["attribution"]["code_counts"])
```

## 目录

- `gbtdiag/`：领域组件源码（分箱、增益、叶权重、提升、精度、注入、
  误差分析、验证器）。
- `tests/`：30 个 `unittest` 用例，覆盖八项业务要求与全部业务码。
- `examples/run_demo.py`：端到端示例，运行前清空 `runs/demo` 并写快照。
- `TECHNICAL_NOTES.md`：架构、确定性契约、误差归因、注入降级与业务码
  的完整技术说明。

## 环境说明

任务目标环境为 Windows 64 位 + Python 3.11。当前实现不使用任何
平台专有 API，在 Linux + Python 3.10 上完成验证；两套环境下固定种子
产出的规范化哈希保持逐字节一致。
