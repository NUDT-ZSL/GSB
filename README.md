# Transformer 激活 + 转置融合算子组件

这是一个不依赖外部服务的本地可集成组件，用于执行 Transformer 前向链路中的
“逐元素激活 → 纯轴重排”相邻算子对。组件在单一分块内核中完成激活与直接重排写出，
避免物化中间激活张量，并以逐算子 float64 路径作为参考真值做逐元素裁决。

## 快速开始（Windows PowerShell）

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install numpy==1.26.4 pytest==8.2.0

python -m transformer_fusion generate --data-dir data
python -m transformer_fusion execute `
  --manifest data\manifest.jsonl `
  --data-dir data `
  --cache-dir cache `
  --snapshot-dir snapshots `
  --json-out artifacts\execution.json

python -m pytest -q
python -m transformer_fusion demo --output-dir artifacts
```

Linux/macOS 下将反斜杠换为 `/` 即可。所有命令均从项目根目录以 `python -m` 方式执行。
运行前组件会检查缓存目录、快照目录可写且可获得跨进程独占锁；失败返回
`E_WORKSPACE_NOT_WRITABLE` 或 `E_WORKSPACE_BUSY`。

## 样例数据

`python -m transformer_fusion generate` 生成：

- `data/manifest.jsonl`：9 个确定性 case，字段包含 `case_id`、`op_chain`、
  `input_shape`、`axis_order`、`activation_kind`、`block_size`、`eps`、`dtype`、
  `seed`、`policy`。
- `data/cases/*.npz`：每个 case 独立输入，键为 `input`。
- `data/inputs.npz`：按 `case_id` 聚合的输入。
- `data/dataset_meta.json`：生成参数与输入内容哈希。

输入由 `numpy.random.default_rng(seed).standard_normal(...)` 生成后按 manifest
精度确定性地 cast，不使用时间或外部数据。

## 核心入口

```python
from transformer_fusion import (
    execute,
    inject_diagnose,
    cache_diagnose,
    equivalence_report,
)

report = execute(
    "data/manifest.jsonl",
    data_dir="data",
    cache_dir="cache",
    snapshot_dir="snapshots",
)

injections = inject_diagnose("data/manifest.jsonl", cache_dir="cache")
cache_report = cache_diagnose(
    "data/manifest.jsonl",
    changes={
        "case_gelu_perm": {"axis_order": [1, 2, 0]},
        "case_silu_eps_axis": {"eps": 1e-3},
    },
    cache_dir="cache",
    snapshot_dir="snapshots",
)
evidence = equivalence_report("data/manifest.jsonl")
```

`execute` 返回 `ExecutionReport`，可用 `report.to_dict()` 获得可序列化报告。
完整可运行示例见 `python -m examples.api_demo`。

## 融合规则

只识别 `op_chain` 中直接相邻的激活/转置对。融合可行的显式条件：

- 激活算子必须在逐元素激活白名单中，且不存在归约依赖；
- 转置算子必须是 `transpose` 或 `permute` 的纯轴重排；
- `axis_order` 必须是 `input_shape` 合法排列；
- 两个算子必须相邻，中间没有归约、转置或其他算子；
- `block_size` 为正、`eps` 有限非负、`dtype` 受支持。

可融合对示例：`["gelu", "transpose"]`。不可融合示例：

- `["gelu", "softmax", "transpose"]`：激活与转置之间存在归约；
- `["softmax", "transpose"]`：`softmax` 不是逐元素激活；
- 无相邻激活/转置对时直接走逐算子路径。

不可融合原因与 `fallback_category`、`upstream_code` 会写入执行计划。

## 两阶段分块内核

输入按显式元素边界 `[start, end)` 切块：

```text
for start in range(0, numel, block_size):
    end = min(start + block_size, numel)
```

对每个输入平坦索引 `i`：

1. 通过 `unravel_index(i, input_shape)` 得到输入坐标 `c`；
2. 计算激活值 `a = activation(x[i])`；
3. 输出坐标唯一确定为 `c[axis_order[0]], ...`；
4. 通过 `ravel_multi_index` 直接 scatter 到输出平坦位置。

融合路径只分配当前块的输入、激活值和目标索引临时数组，不分配激活大小的完整中间张量，
也不把中间激活写入缓存或磁盘。逐算子路径会物化完整激活张量，然后执行 `np.transpose`，
作为 float64 参考。

## 数值裁决

- 融合路径与逐算子路径均在 float64 下执行核心计算；
- 中间激活摘要按输入序逐块哈希，两条路径必须相同；
- 两条路径最终 cast 到任务精度后的输出必须逐字节相同；
- 裁决以逐算子 float64 输出为参考，计算最大绝对偏差、最大相对偏差、
  越界元素坐标、输入块号和偏差谱；
- 超过 `abs_tol + rel_tol * abs(reference)` 返回 `E_TOLERANCE_EXCEEDED`，
  并给出 `activation_precision_cast`、`activation_semantics`、
  `axis_reorder_mapping` 或 `block_boundary_write` 一级归因。

默认容差：fp16 `(5e-3, 5e-2)`，fp32 `(1e-6, 1e-5)`，fp64 `(1e-12, 1e-10)`。

## 异常注入

`inject_diagnose` 在独立缓存/快照命名空间运行 baseline 与注入 case，直接修改参数或输入副本，
不修改源码：

| 环节 | 注入 | 业务码 |
|---|---|---|
| 算子链解析 | `op_chain` 追加未知算子 | `E_OP_UNKNOWN` |
| 融合可行性 | `axis_order` 重复轴 | `E_AXIS_INVALID` |
| 融合内核 | 输入置为 NaN/Inf | `E_NON_FINITE_INPUT` |
| 一致性裁决 | 非正容差 | `E_TOLERANCE_CONFIG` |
| 一致性裁决 | 极紧容差 | `E_TOLERANCE_EXCEEDED` |

每条记录包含 `case_id`、`stage`、`business_code`、明细和未注入 case 隔离校验结果。
样例注入配置见 `examples/injections.json`。

## 内容寻址缓存与部分失效

融合产物 key 的 canonical JSON 包含：

- 输入张量内容 SHA-256；
- 算子链摘要；
- `axis_order`、`input_shape`；
- `block_size` 与执行时的 effective block size；
- `eps` 的 `float.hex()`；
- `dtype` 与 precision tier；
- 激活类型。

key 相同复用已有 artifact，key 不同重算。缓存 artifact、entry、index 均带版本和 SHA-256
校验和。失效依赖分为：

- `input`；
- `activation`：依赖输入、激活、eps、block、dtype；
- `axis_mapping`：只依赖形状、轴序和算子链；
- `transpose`、`fused`、下游节点、`final`。

因此轴序变化复用 `activation`、失效 `axis_mapping` 及融合产物/下游；仅 eps 变化复用
`axis_mapping`、失效激活相关产物。`cache_diagnose` 输出受影响 case、失效/复用节点集合、
重算后缀、key 对照和并行重算波次，并检查两个集合无交集且并集覆盖全部节点。

## 资源与快照

执行计划显式记录：

- 融合减少 1 次中间写和 1 次完整遍历；
- 融合块驻留内存高于逐算子块；
- 轴重排局部性分数；
- 策略优先级 `hard_resource_limit → policy_priority → memory → locality → traversal_count`。

策略支持 `fusion_first`、`balanced`、`memory_first`、`locality_first`。
资源超限时按配置返回 `E_RESOURCE_LIMIT`，或执行显式退回：缩小分块、扩展分块、
降低精度档、退回逐算子。退回记录类别和上游业务码，且仍完成全部 case 的裁决。

执行计划、缓存条目和结果快照均携带单调递增版本号与内容校验和。每次提交整体原子替换
JSON/NPY 文件而非增量修补。版本或校验和不一致返回 `E_SNAPSHOT_CORRUPT`，并指明 `case_id`。

## 实际验收结果

本工作区已运行：

```text
python -m pytest -q
............                                                             [100%]

python -m transformer_fusion demo --output-dir artifacts
ok=true, case_count=9, fused_path_count=4, warm_cache_hits=4
max_abs_deviation_vs_float64=4.185214419472061e-08
```

注入观测码：`E_OP_UNKNOWN`、`E_AXIS_INVALID`、`E_NON_FINITE_INPUT`、
`E_TOLERANCE_CONFIG`、`E_TOLERANCE_EXCEEDED`；所有未注入 case 的输出与 key 不变。
部分失效同时验证了下游 `layernorm` 节点失效、轴序变化复用激活、eps 变化复用轴映射。

机器可读证据：

- `artifacts/acceptance_report.json`
- `artifacts/acceptance_summary.md`
- `artifacts/execution_cold.json`
- `artifacts/execution_warm.json`
- `artifacts/injections.json`
- `artifacts/cache_diagnosis.json`
- `artifacts/equivalence.json`
- `artifacts/snapshot_corruption.json`

## 目录

```text
transformer_fusion/
  engine.py         统一执行与裁决入口
  manifest.py       清单解析、算子识别、可行性规则
  kernel.py         融合分块内核与逐算子参考
  planning.py       融合取舍与资源退回
  cache.py          内容寻址缓存、key、部分失效
  snapshot.py       版本化计划与结果快照
  injection.py      四阶段异常注入
  selfcheck.py      跨模块一致性自检
  generate_data.py  确定性样本生成
  demo.py           完整验收示例
tests/              12 个 pytest 用例
data/               JSONL 清单与 NPZ 样本
docs/TECHNICAL.md   设计与实现说明
examples/           API/注入/变更配置示例
```
