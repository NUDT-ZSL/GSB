# sampling-ops

面向本地 LLM 推理采样链路的确定性 TopK + 采样算子组件。项目从固定种子在运行时生成 logits，不依赖模型权重、网络、外部数据集或容器。

核心能力：

- 三条精确 TopK 选路：`full_sort`、`heap_select`、`chunked_two_stage`。
- 统一平局口径：分数降序；同分按词表索引升序。
- 按规模与资源上限选择一条选路，并输出逐字节可复现的决策记录。
- 温度缩放、包含边界的 top-p 截断、局部 PCG64 确定性采样。
- float64 全排序参考真值、候选/分数/样本偏差统计与容差裁决。
- 四阶段无源码修改异常注入：候选筛选、温度缩放、top-p 截断、采样。
- 跨选路候选核对、样本重放自检、配置/统计量版本化快照。
- 可集成 Python API 与可直接运行的 JSONL CLI 示例。

## 环境

- Python 3.10+
- NumPy 1.24+
- 可写工作目录

```bash
python3 -m pip install -e .
python3 -m unittest discover -s tests -v
```

不访问网络，不需要 Docker、GPU 或真实模型。

## 快速运行

```bash
# 生成确定性任务清单
python3 -m sampling_ops generate --out artifacts/tasks.jsonl

# 生成清单、运行完整示例、写出 JSON 与版本化快照
python3 -m sampling_ops demo \
  --out artifacts/demo_output.json \
  --tasks-out artifacts/tasks.jsonl \
  --cache-dir artifacts/cache \
  --pretty

# 对已有 JSONL 运行并输出完整结构化结果
python3 -m sampling_ops run \
  --tasks artifacts/tasks.jsonl \
  --out artifacts/run_output.json \
  --cache-dir artifacts/cache \
  --pretty

# 输出紧凑的自检报告
python3 -m sampling_ops selfcheck \
  --tasks artifacts/tasks.jsonl \
  --out artifacts/selfcheck_output.json \
  --pretty
```

`demo_output.json` 的每个 case 都包含输入、选路决策、三条选路候选、主选路样本序列、偏差裁决、自检、四阶段 trace、注入记录、业务码与告警。

## Python API

```python
import numpy as np

from sampling_ops import decide_route, run_task, sample, topk

logits = np.asarray([[0.2, 1.1, 0.8, 1.1, -0.4]], dtype=np.float64)

# 单条选路
candidates = topk(
    logits,
    k_value=3,
    route="heap_select",
    tie_policy="index_asc",
    case_id="example",
)
print(candidates.indices)  # [[1, 3, 2]]，同分索引 1 在 3 前

# 温度 -> top-p -> 局部 RNG 采样
drawn = sample(
    candidates,
    temperature=0.8,
    top_p=0.9,
    seed=2026,
    group_size=4,
    case_id="example",
)
print(drawn.samples)

# 决策记录（同一输入逐字节一致）
decision = decide_route(
    4096,
    3,
    16,
    "float32",
    resource_profile={
        "max_compare": 100_000,
        "max_memory_bytes": 8 * 1024 * 1024,
        "compare_soft_ratio": 0.7,
        "chunk_size": 512,
    },
    case_id="example",
)
print(decision["selected_route"])

# 完整四阶段入口：任务清单，logits 在运行时生成
result = run_task(
    {
        "case_id": "example",
        "vocab_size": 128,
        "batch_size": 2,
        "seed": 42,
        "k_mode": "auto",
        "k_value": 8,
        "temperature": 0.9,
        "top_p": 0.92,
        "group_size": 3,
        "dtype": "float32",
        "tie_policy": "index_asc",
        "resource_profile": "small",
    }
)
print(result["business_code"], result["self_check"]["business_code"])
```

`group_size` 定义为每个 batch row 的采样步数。输出样本形状为 `(batch_size, group_size)`。

## 任务清单

JSONL 每行一个对象。必选字段：

| 字段 | 含义 |
|---|---|
| `case_id` | 业务 case 标识 |
| `vocab_size` | 词表大小 |
| `batch_size` | batch 行数 |
| `seed` | logits 生成与采样 seed |
| `k_mode` | `auto` 或强制 `full_sort` / `heap_select` / `chunked_two_stage` |
| `k_value` | TopK 大小，必须 `1 <= k_value <= vocab_size` |
| `temperature` | 有限正数 |
| `top_p` | `(0, 1]` |
| `group_size` | 每个 batch row 的采样步数 |
| `dtype` | `float16` / `float32` / `float64` 及 `fp16` / `fp32` / `fp64` |
| `tie_policy` | `index_asc`、`index_ascending`、`stable_index` 或 `score_desc_index_asc` |
| `resource_profile` | 资源对象或 `default` / `small` / `balanced` / `compare_tight` / `memory_tight` |

可选字段：

- `injections`：注入列表，见下文。
- `logits_quantum`：demo 用分数量化步长，用于稳定构造平局样本。
- `tolerance`：偏差容差，默认 `1e-12`。

资源对象支持：

```json
{
  "max_compare": 100000,
  "max_memory_bytes": 8388608,
  "compare_soft_ratio": 0.7,
  "memory_soft_ratio": 0.8,
  "chunk_size": 512
}
```

别名包括 `max_comparisons`、`comparison_budget`、`memory_limit_bytes`、`max_resident_bytes`、`chunk_elements` 等。

## TopK 与平局规则

三条选路必须产生完全相同的候选索引、索引顺序与候选分数口径：

1. `full_sort`：稳定全排序，比较估计 `batch * n * ceil(log2(n))`。
2. `heap_select`：显式最小堆，键为 `(score, -index)`，堆容量 `k`。
3. `chunked_two_stage`：块内稳定 TopK，随后流式合并到全局容量为 `k` 的堆；块大小至少为 `k`。

最终顺序统一为：

```text
score descending; equal score -> vocabulary index ascending
```

候选缺失、重复或路径间首个位置不一致会返回 `E_TOPK_INCONSISTENT`，记录 `case_id`、`route` 和首个偏差位置。

## 选路决策与冲突裁决

决策器在候选执行前锁定选路，不会在失败后改选其他路径。优先级：

1. 保持候选与平局口径正确。
2. 过滤超过比较/内存硬上限的选路。
3. 全排序比较估计超过比较软上限时，优先堆选择。
4. 全排序驻留估计超过内存软上限时，选择内存受限路径。
5. 大 `k` 的强内存压力下，优先分块两阶段，并显式记录合并边界开销。
6. 剩余平局用固定路径名顺序裁决。

决策记录包含：

- 规模观测：`vocab_size`、`batch_size`、`k_value`、dtype、chunk size。
- 三条路径的比较次数估计、峰值驻留估计、公式与边界开销。
- 可行路径、软/硬上限、所选路径、降级类别、理由码。
- 冲突取舍明细。
- 规范化 JSON 的 SHA-256，便于逐字节比较。

如果所有路径都超过硬上限，仍选择“归一化越界最小”的路径执行，标记 `RESOURCE_BUDGET_OVERRUN`，并继续输出可核对候选与样本。此时结果带 `W_RESOURCE_OVERRUN`，但 `business_code` 仍为 `OK`。

## 采样口径

阶段顺序固定：

```text
TopK candidates
-> score / temperature
-> float64 stable softmax
-> inclusive cumulative top-p
-> inverse-CDF sampling with local PCG64
```

边界规则：

- top-p：保留累计概率首次大于等于 `top_p` 的最短前缀；等号时包含边界 token。
- 采样：`searchsorted(cdf, u, side="right")`；`u` 恰为内部累计边界时选择下一个 token。
- RNG：`numpy.Generator(PCG64(SeedSequence(...)))`，不读取或修改 NumPy 全局随机状态。
- 同一候选、temperature、top-p、seed、group_size 下样本序列逐字节可复现。

top-p 只限制采样 nucleus，不修改已输出的 TopK 候选；被裁掉的候选数与降级类别会记录在 `conflict_resolution.top_p_truncation`。

## 业务码

| 业务码 | 触发条件 |
|---|---|
| `E_DTYPE_UNSUPPORTED` | 未知 dtype 或非浮点 logits |
| `E_SHAPE_INVALID` | logits 形状与 `batch_size`/`vocab_size` 不符 |
| `E_K_INVALID` | `k_value <= 0` 或超过 `vocab_size` |
| `E_TOPK_INCONSISTENT` | 候选缺失、重复或三路首个偏差 |
| `E_TEMPERATURE_INVALID` | temperature 非有限、零或负 |
| `E_TOP_P_INVALID` | top_p 非有限或不在 `(0, 1]` |
| `E_SEED_INVALID` | seed 缺失、布尔或非整数 |
| `E_TOLERANCE_EXCEEDED` | 候选索引/分数/样本超过参考容差 |
| `E_INCONSISTENT` | 跨选路或样本重放自检失败 |
| `E_SNAPSHOT_INVALID` | 快照损坏或校验和不符 |
| `E_SNAPSHOT_VERSION_REGRESSION` | 内容变化但请求版本不递增 |

## 四阶段异常注入

注入可通过任务 JSONL 的 `injections` 字段，或 CLI `--inject '<json>'` / `--injection-file` 触发，不需要修改源码。

```bash
python3 -m sampling_ops run \
  --tasks artifacts/tasks.jsonl \
  --out artifacts/injected.json \
  --inject '{"stage":"temperature_scale","type":"temperature_zero"}'
```

内置类型：

| 类型 | 阶段 | 字段 | 预期业务码 |
|---|---|---|---|
| `non_finite_logits` | `candidate_filter` | `logits` | `E_LOGITS_NON_FINITE` |
| `temperature_zero` / `temperature_negative` | `temperature_scale` | `temperature` | `E_TEMPERATURE_INVALID` |
| `top_p_out_of_range` / `top_p_zero` | `top_p_filter` | `top_p` | `E_TOP_P_INVALID` |
| `seed_missing` | `sample` | `seed` | `E_SEED_INVALID` |

注入记录包含 `case_id`、`stage`、`field`、`applied`、`status`、`business_code`。上游失败时，下游阶段标为 `skipped`；未归因的框架级异常写入 `unattributed_errors`。

## 偏差与自检

参考真值始终是 float64 全排序路径，不使用放宽容差或跳过路径的方式掩盖差异。

每个成功 case 输出：

- 参考候选索引与分数。
- 各路径候选索引是否一致。
- 各路径逐元素候选分数偏差、最大绝对偏差。
- 各路径样本序列是否与参考一致。
- 首个越界元素，格式如 `batch[0].candidate[2]`。

自检还会：

- 核对三条路径候选哈希/顺序一致。
- 用每条路径自己的候选和同一 seed 重放样本，并与已存样本比较。
- 失败时返回 `E_INCONSISTENT` 和首个失败位置。

## 版本化快照

`SnapshotStore` 将 `config` 与 `stats` 分别写为完整快照：

```json
{
  "kind": "config",
  "version": 1,
  "algorithm": "sha256",
  "checksum": "...",
  "content": {}
}
```

- 内容不变时版本不变。
- 内容变化时版本单调递增，并整体替换旧快照，不做字段合并。
- 版本回退请求返回 `E_SNAPSHOT_VERSION_REGRESSION`。
- 加载时校验内容 SHA-256，损坏返回 `E_SNAPSHOT_INVALID`。

## 验证

当前工作区最近一次结果：

```text
python3 -m unittest discover -s tests -v
Ran 13 tests in 0.515s
OK

demo summary:
case_count=9
status_counts={"INJECTED_ERROR":4,"OK":5}
selected_route_counts={"chunked_two_stage":1,"full_sort":6,"heap_select":2}
bias_status_counts={"OK":5,"skipped":4}
self_check_status_counts={"OK":5,"skipped":4}
max_abs_score_deviation=0.0
injection_stage_counts={"candidate_filter":1,"sample":1,"temperature_scale":1,"top_p_filter":1}
non_injected_error_cases=[]
```

完整输出在 `artifacts/demo_output.json`，任务清单在 `artifacts/tasks.jsonl`，快照在 `artifacts/cache/`。详细设计与验证记录见 `docs/DESIGN.md` 和 `docs/VALIDATION.md`。

