# 人工边界样本

这些 JSON 均为手工构造，不依赖生成器，既可单独传给 `solve`，也会被 `python3 -m qindex boundaries` 自动执行断言。

| 文件 | 用途 |
| --- | --- |
| `01_exact_flush.json` | 刷写阈值恰为 3，三条同刻事件必须进入同一段 |
| `02_invalid_ratio_exact.json` | 段内失效比例恰为 `0.5`，验证 `>=` 阈值 |
| `03_archived_query.json` | 归档段未合并仍存活，查询仍须过滤全部失效条目 |
| `04_capacity_only.json` | 无失效条目，仅由分区容量触发合并 |
| `05_ratio_and_capacity.json` | 比例与容量同时满足，验证固定的比例优先决策 |

每个文件顶部的 `sample_expectations` 是人工期望说明；实际断言逻辑位于 `qindex/boundaries.py`。
