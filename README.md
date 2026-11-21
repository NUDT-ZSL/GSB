# IGEVAL：确定性图文生成质量评估组件

`igeval` 是一个无网络依赖、可嵌入本地服务的文生图质量评估领域组件。它从固定种子和固定画布渲染本地样本，执行四级评估流水线，并同时提供：

- 批量全量评估（`ProcessPoolExecutor` + `spawn`）
- 按样本 id 的单点查询（精确同口径 + 内容寻址缓存 + 延迟观测）
- 四级分数的版本化快照与原子整体替换
- 提示词修正、样本追加、快照切换后的缓存失效与重算归因
- 批量/单点/缓存/排序/失效覆盖的跨模块自检

核心设计是：**批量和单点调用同一个 `run_pipeline` 精确内核**。单点缓存只复用完全相同输入、相同快照版本和相同精度配置下的结果；缓存命中不会改变分数、摘要或排序。时间只写入观测元数据，不进入任何评分或内容摘要。

## 快速开始

要求 Python 3.10+、NumPy，Windows/Linux 均可。

```bash
python3 -m pip install -r requirements.txt
python3 -m pip install -e .
python3 -m unittest discover -s tests -v
python3 -m igeval.demo
```

Windows PowerShell：

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -e .
python -m unittest discover -s tests -v
python -m igeval.demo
```

示例会生成：

- `artifacts/demo_report.json`：完整、机器可核对的验收报告
- `artifacts/demo_summary.txt`：人类可读摘要
- `artifacts/demo_runtime/snapshots/`：版本化快照文件与原子 `CURRENT`
- `artifacts/demo_runtime/cache/entries/`：按缓存键命名的内容寻址条目

## API

```python
from igeval import (
    EvaluatorConfig,
    QualityEvaluator,
    build_demo_samples,
)

samples = build_demo_samples(count=12, width=64, height=64)
config = EvaluatorConfig(
    workers=4,
    latency_budget_ms=100.0,
    max_cache_entries=128,
    max_feature_memory_bytes=8 * 1024 * 1024,
    precision="exact",
)
evaluator = QualityEvaluator(
    samples,
    config=config,
    runtime_dir="artifacts/runtime",
)

batch = evaluator.evaluate_batch()
point = evaluator.query("sample-001", latency_budget_ms=100.0)
snapshot = evaluator.get_snapshot()
invalidation = evaluator.invalidation_report()
check = evaluator.self_check(batch)
```

变更入口：

```python
switch_result = evaluator.switch_snapshot(new_complete_snapshot)
prompt_result = evaluator.update_prompt("sample-001", "revised prompt")
append_result = evaluator.append_samples([new_sample])
```

`evaluate_batch`、`query`、`switch_snapshot`、`update_prompt`、`append_samples` 都返回结构化字典；业务失败不依赖异常文本解析。配置对象本身的非法参数会抛出 `DomainError`，并提供 `code`、`field` 等字段。

## 四级流水线

1. **预处理**：确定性面积平均下采样至 `16x16`，计算 RGB 均值/方差、亮度直方图与饱和度，并划分为 `4x4` 个图像块。每块形成 12 维归一化特征。阶段分为对比度、饱和度、色彩平衡的确定性函数。
2. **语义对齐**：对提示词和参考描述做 NFKC + 小写 + 固定正则分词。每个词项由 SHA-256 字节确定性地映射为 12 维单位向量；与每个图像块特征计算余弦相似度，取最佳块。词项权重由快照中的对齐权重和位置衰减决定。
3. **结构一致性**：在亮度图上计算梯度边缘密度、横纵方向平衡和 4x4 区域亮度分布。目标区域分布和边缘/方向目标由文本词项摘要与结构参数版本确定性导出；按快照中的边缘、区域、方向权重聚合相似度。
4. **归一化与聚合**：按快照中的融合权重合并前三阶段分数，再使用快照中的均值/标准差计算 z-score 和 sigmoid，得到 `[0, 1]` 最终质量分和 1–5 预测分档。

固定精度为 10 位小数。所有阶段分数、中间特征和摘要均可序列化为规范 JSON。`score_digest` 不包含计时、缓存命中状态或任何系统时间；相同样本、配置和快照下逐字节一致。

评分参数来自不可变快照。`cache_key` 明确包含：

- 图像像素内容哈希
- 文本词项摘要
- 样本 id（防止相同内容的异构样本相互覆盖）
- 对齐权重版本与校验和
- 归一化统计量版本与校验和
- 结构参数版本与校验和
- 整体快照 id、`precision="exact"`、评分配置指纹

## 快照与原子切换

一个完整快照包含三个独立组件，每个都有单调递增版本号和内容校验和：

- `normalization`：均值、标准差、融合权重
- `alignment`：提示词/参考词项权重、位置衰减
- `structure`：边缘/区域/方向权重、目标范围与容差

提交时必须传入完整快照。切换流程先校验所有组件校验和和整体校验和，再在状态锁内检查版本单调性、原子写入版本文件并替换 `CURRENT`，最后替换内存中的不可变快照引用。查询在开始时一次性捕获快照引用，因此不会混用新旧统计量。

- 版本回退或同版本覆盖不同内容：`E_SNAPSHOT_ROLLBACK`
- 组件或整体校验和不符：`E_SNAPSHOT_CORRUPT`
- 新快照生效后，所有样本缓存失效，单点查询读取新版本

`evolve_snapshot` 用于从当前快照生成新的完整快照，不执行增量修补。

## 查询延迟与资源上限

`query` 用本地评估循环的 `perf_counter_ns` 测实际耗时，并返回：

- 像素数、分块数、对齐候选数
- 线性成本模型的估算毫秒数
- 实际观测毫秒数、预算、是否超限
- 已完成阶段标记

超过预算返回 `E_LATENCY_BUDGET`，并显式标记 `LATENCY_BUDGET_EXCEEDED`。此时四级结果仍会返回，便于归因和核对，但 `status` 为 `error`。

驻留缓存条目数和驻留特征内存可配置。超限返回 `E_RESOURCE_LIMIT`，并标记：

- `CACHE_ENTRY_LIMIT`
- `FEATURE_MEMORY_LIMIT`
- `DISK_CACHE_IO`

本组件只支持声明为 `exact` 的精度。未知精度返回 `E_UNKNOWN_PRECISION`，不存在未声明的近似降级。

## 缓存与失效

单点查询缓存完整的精确结果、`feature_digest` 和各级分数，并以缓存键作为内容寻址文件名。重复查询可直接命中；`use_cache=False` 强制重算用于审计。

失效事件分别记录：

- 快照切换：全部样本受影响
- 提示词修正：被修正样本受影响
- 样本追加：新增样本受影响

每个事件包含 `affected_sample_ids`、`invalidated_cache_keys`、`cache_hit_sample_ids`、`recomputed_sample_ids` 和 `pending_sample_ids`。缓存命中与重算集合不相交，完成重算后二者并集等于受影响集合。

## 并发与一致性

- 批量使用 `multiprocessing.get_context("spawn")` 和 `ProcessPoolExecutor`，兼容 Windows。
- 单点查询可由多个线程并发调用。
- 快照切换、提示词更新、样本追加与查询捕获共享状态锁。
- 计算完成后仅在快照和样本修订仍为当前值时写入缓存，防止旧结果覆盖新版本。
- 同分排序按 `(-final_score, sample_id)`，即同分时样本 id 升序。
- 自检重新执行冷查询并与批量结果逐级比较；发现偏差返回 `E_INCONSISTENT`，并报告首个偏差的级与样本 id。

自检检查项包括：

- 四级各自处理的样本 id 集合
- 各级和最终分数区间
- 聚合排序与同分裁决
- 批量、冷查询、热缓存查询的 `score_digest`
- 缓存键、特征摘要、缓存条目内容
- 每个失效事件的命中/重算集合

失败类别包括 `score_mismatch`、`stage_sample_set`、`score_range`、`aggregation_order`、`digest_mismatch`、`cache_key_feature_digest`、`invalidation_coverage`、`snapshot_mismatch` 等。

## 错误码

| 业务码 | 触发条件 | 关键字段 |
| --- | --- | --- |
| `E_DUPLICATE_SAMPLE_ID` | 样本 id 重复 | `sample_id` |
| `E_CANVAS_MISMATCH` | 种子、画布、实际图像尺寸或渲染签名不一致 | `canvas` |
| `E_EMPTY_PROMPT` | 提示词为空或没有语义词项 | `prompt` |
| `E_UNKNOWN_PRECISION` | 精度配置未知 | `precision` |
| `E_INVALID_LATENCY_BUDGET` | 延迟预算非正或非有限数 | `latency_budget_ms` |
| `E_INVALID_MEMORY_LIMIT` | 缓存/内存上限非正 | 对应上限字段 |
| `E_LATENCY_BUDGET` | 实测耗时超出预算 | 完成阶段、估算记录 |
| `E_SNAPSHOT_ROLLBACK` | 版本回退或同版本覆盖不同内容 | 首个冲突字段 |
| `E_SNAPSHOT_CORRUPT` | 快照校验和或结构损坏 | 组件/校验和字段 |
| `E_RESOURCE_LIMIT` | 驻留条目或特征内存超限 | `degradation_category` |
| `E_INCONSISTENT` | 自检发现跨模块不一致 | `category`、`sample_id`、`stage` |
| `E_STAGE_FAILURE` | 阶段内部意外异常 | `stage`、异常类型 |
| `E_UNATTRIBUTED` | 无法归因到具体阶段的异常 | 汇总到 `unattributed_errors` |

样本级错误始终带 `sample_id` 和阶段/验证归因，不会静默丢弃；未归因异常进入批量的 `unattributed_errors`。

## 本次实际验证

本次容器为 WSL2（原生 Windows 解释器未启用 WSL interop），以下为实际运行 Python 3.10.12、NumPy 1.26.4 的结果：

```text
status: PASS
workers: 4 (spawn process pool for batch)
samples: 12; final samples: 13

initial batch: ok
query digests match batch: True
all warm queries cache hits: True
initial self-check: ok

cold median/p95 ms: 20.496374 / 23.285042
warm median/p95 ms: 3.649158 / 3.981517
spawn worker PIDs: 4 distinct processes
forced budget code: E_LATENCY_BUDGET
completed stages: preprocess, semantic, structure, quality

snapshot switch: ok -> v2
affected before recompute: 12
final coverage complete: True
rollback probe: E_SNAPSHOT_ROLLBACK
corruption probe: E_SNAPSHOT_CORRUPT
final self-check: ok
```

自动化测试：`22` 项全部通过，其中包含独立子进程启动 `spawn` 进程池的端到端测试。两次独立示例运行的初始批量 `score_digest`、`feature_digest`、各级分数逐字节一致。原始输出保存在 `artifacts/test_results.txt`、`artifacts/demo_stdout.txt` 和 `artifacts/determinism_check.txt`。

Windows 兼容性由以下实现保证：不使用 `fork`、不使用 POSIX 专用锁、文件替换使用 `os.replace`、进程池入口为模块级可序列化函数、示例有 `if __name__ == "__main__"` 保护。原生 Windows 上执行 `python -m igeval.demo` 即使用同一代码路径。

详细契约见 `docs/DESIGN.md`，实际报告见 `artifacts/demo_report.json`。

