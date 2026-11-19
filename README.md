# FlashAttention 风格分块注意力组件

这是一个仅依赖 Python 3.10+ 与 NumPy 的本地可集成组件。它把多头注意力展开成确定性的分块 DAG，每个节点产物带内容哈希并写入本地内容寻址缓存。K/V 按块携带修订号；某个块被替换或追加时，只重算该块及依赖它的打分、在线 softmax 和累加后缀。

不使用 GPU、Docker、容器或外部服务。并行执行使用跨平台的 `ThreadPoolExecutor`；每个节点内部是 NumPy 向量化计算，缓存写入使用跨线程/跨进程文件锁与原子整文件替换。

## 快速开始

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .

python -m scripts.generate_samples --output samples
python -m scripts.run_validation
python -m examples.run_demo
```

Windows 激活虚拟环境使用 `.venv\Scripts\activate`。也可以不安装项目，直接从仓库根目录以模块方式运行。
若系统没有自带的 `pip/ensurepip`，可用 `uv venv --python 3.10 .venv` 与 `uv pip install --python .venv/bin/python -e .` 完成同样准备。

生成结果：

- `samples/cases.jsonl`：固定种子任务清单。
- `samples/arrays/*.npy`：形状为 `(n_heads, seq_len, head_dim)` 的 Q/K/V。
- `samples/manifest.json`：样本与命令行生成文件哈希。
- `artifacts/validation_report.json`：单元测试实际结果。
- `artifacts/demo_report.json`：基线、增量、全量重算与缓存诊断的实际结果。

## 集成接口

```python
from pathlib import Path

from flash_attn_cache import (
    FlashAttentionEngine,
    QKVArrays,
    RuntimeConfig,
)
from flash_attn_cache.samples import load_samples

case, arrays = load_samples("samples")[0]
engine = FlashAttentionEngine(
    Path("cache/production"),
    runtime=RuntimeConfig(
        parallel_nodes=4,
        max_parallel_nodes=4,
        max_resident_bytes=256 * 1024 * 1024,
        max_cache_entries=20_000,
    ),
)

baseline = engine.compute(case, arrays)
assert baseline.ok
print(baseline.output.shape)  # (n_heads, seq_len, head_dim)

updated_case = case.replace(kv_revision=(1, 0, 0))
updated_k = arrays.k.copy()
updated_k[:, 0:case.block_size, :] += 0.2
updated_arrays = QKVArrays(arrays.q, updated_k, arrays.v)

incremental = engine.incremental_update(
    case,
    arrays,
    updated_case,
    updated_arrays,
    baseline_result=baseline,
)
assert incremental.ok
print(incremental.recomputed_nodes)
print(incremental.reused_nodes)

invalidation = engine.invalidation_report(
    case,
    arrays,
    updated_case,
    updated_arrays,
    incremental_result=incremental,
)
diagnostics = engine.diagnose_cache()
self_check = engine.self_check(
    updated_case,
    updated_arrays,
    incremental.attention,
    incremental=incremental,
)
```

`AttentionCase.kv_revision` 可为：

- 标量：所有 K/V 块共用同一修订号。
- 列表/元组：元素数必须等于 K/V 块数，逐块表示修订号。

块内容变化但修订号未变也会被检测并失效；修订号变化但内容不变同样会使依赖链重算，以保持显式修订语义。

## 计算图

对每个序列块 `i`、K/V 块 `j`，图包含：

1. `split_q:i`、`split_kv:j`：确定性分块与 float64 化。
2. `qproj:i`：确定性恒等投影/连续化。这里没有学习权重，闭包只由显式任务和种子确定。
3. `kvload:j`：载入 K/V 块并纳入 `kv_revision`。
4. `score:i:j`：`Q_i K_j^T / sqrt(head_dim)`，因果模式写入块内与跨块掩码。
5. `softmax:i:j`：每个块的行最大值 `m_ij`、指数和 `l_ij` 与权重。
6. `accum:i:j`：把 `(m_ij, l_ij, P_ij V_j)` 与前一累加状态合并。
7. `finalize:i`：`O_i = acc_i / l_i`。

在线合并公式：

```text
m_new = max(m_prev, m_chunk)
l_new = l_prev * exp(m_prev - m_new)
      + l_chunk * exp(m_chunk - m_new)
acc_new = acc_prev * exp(m_prev - m_new)
        + (P_chunk @ V_chunk) * exp(m_chunk - m_new)
```

因果图只连接 `j <= i` 的有效块；非因果图连接全部 K/V 块。所有块计算与累加均使用 float64。输出始终为 C 连续 float64 数组。

`SoftmaxConfig` 支持：

- `subnormal_floor`：有限正数归一化因子的下限；默认 `1e-300`。
- `rounding_mode`：`nearest` 或 `truncate`。
- `rounding_precision`：保留的尾数位数，`0..52`，默认 `52`。
- `atol`、`rtol`：自检容差。

负无穷掩码位置不会参与有效行最大值；归一化因子非有限返回 `E_SOFTMAX_NAN`，为零且未启用下限保护返回 `E_NORM_ZERO`。

## 缓存键与快照

节点内容哈希由以下部分规范化为 JSON/字节后计算 SHA-256：

- 节点种类、查询块/K/V 块序号。
- `head_dim`、`n_heads`、`block_size`、`causal`、掩码语义、dtype、seed、scale。
- 节点参数，例如块起止位置、长度、K/V 修订号、softmax 策略。
- 源分块内容哈希。
- 上游节点的 `content_hash` 与递归 `cache_key`。

递归 `cache_key` 是 Merkle 式谱系键：即使修订后数值内容恰好不变，上游 `kvload` 键变化仍会使下游打分、softmax、累加和 finalize 失效，避免静默复用。

缓存文件包含：

- 单一原子快照文件，格式为 magic + 头长度 + 规范 JSON 头 + 原始数组字节。
- 单调递增 `key_version` 与同目录版本标记。
- 数组/元数据 `content_hash`。
- 覆盖完整头与数据载荷的 `checksum`。
- 写入时先写临时文件、`fsync`、再 `os.replace` 整体替换。

读取期间持有同一个逐键锁，不会看到半更新文件。八个并发任务写同一键时只有一个实际写入生效。损坏、版本不匹配或校验和错误返回 `E_CACHE_CORRUPT`；执行器记录恢复性错误，并从该节点开始的整段下游强制重算，禁止使用部分可解析的损坏数据。

## 增量失效

更新入口比较前后 K/V 块的内容哈希和修订号，也检测 Q 块追加/变化：

1. 生成失效根：`split_kv:j`、`kvload:j`，以及必要时的 `split_q:i`、`qproj:i`。
2. 沿 DAG 求后代闭包，得到受影响节点集合。
3. 未受影响节点直接命中缓存。
4. 受影响打分节点重算，其在线 softmax 与累加链只重算对应的后缀。
5. 返回重算清单、复用清单、受影响清单、变更块号和根节点。

自检验证重算集合与复用集合无交集，且二者并集覆盖全部受影响节点。

## 异常与资源

主要业务码：

| 业务码 | 含义 |
| --- | --- |
| `E_DUPLICATE_CASE_ID` | 批次内 `case_id` 重复 |
| `E_SEQ_LEN_MISMATCH` | Q/K/V 形状与 `seq_len`/头配置不一致 |
| `E_INVALID_HEAD_DIM` | `head_dim` 非正 |
| `E_INVALID_BLOCK_SIZE` | `block_size` 非正 |
| `E_CAUSAL_MASK_CONFLICT` | `causal` 与 `mask_mode` 冲突 |
| `E_UNSUPPORTED_DTYPE` | dtype 不是 float32/float64 |
| `E_SOFTMAX_NAN` | 分数、统计量、累加量或输出非有限 |
| `E_NORM_ZERO` | 归一化因子为零且无下限保护 |
| `E_CACHE_CORRUPT` | 文件损坏、校验和/版本/内容哈希不符 |
| `E_RESOURCE_LIMIT` | 内存估算、缓存条目或锁等待超限 |
| `E_INCONSISTENT` | 图、键、集合、统计量或输出自检不一致 |

节点异常附带 `case_id`、`node_id`、块序号和分类。公共入口捕获未归因异常并写入 `unattributed_errors`，不会静默吞掉。

资源控制：

- `max_resident_bytes`：根据一次调用全部节点输出数组与输入大小保守估算；超限直接返回 `E_RESOURCE_LIMIT`。
- `max_cache_entries`：写入前在全局缓存条目锁内检查；超限返回 `E_RESOURCE_LIMIT`。
- `parallel_nodes` / `max_parallel_nodes`：请求并发高于上限时降级到上限，并在结果中标记 `parallelism_limited`。

## 自检

`engine.self_check(...)` 检查：

- DAG 拓扑无环且覆盖所有节点。
- 每个节点产物的内容哈希可由数组与元数据重算。
- 每个缓存键可由上游键/内容哈希重算，缓存条目版本与校验和匹配。
- 重算/复用集合不交且覆盖受影响节点。
- 由分块打分与 V 明细重组的输出与最终输出一致。
- 合并后的分块 softmax 分母与整段分母一致。

任一不一致返回 `E_INCONSISTENT`，并给出 `category`、`case_id`，相关时给出 `node_id` 与块序号。

## 实际验证结果

运行环境：Python 3.10.12、NumPy 2.2.6、Linux/WSL 本地 CPU。

`artifacts/validation_report.json` 记录 26 个测试全部通过。覆盖：

- 三个固定种子样本与稠密参考及自检。
- 并行度 1/4 下全部中间数组逐字节一致。
- 重复调用全部缓存命中。
- 修订号变化与 K/V 内容替换的失效传播。
- 非因果 K/V 增长追加的后缀累加重算。
- 缓存损坏恢复、版本单调递增、八线程同键单写。
- 内存、缓存条目、并发上限；`E_SOFTMAX_NAN`、`E_NORM_ZERO` 与输入校验。

`artifacts/demo_report.json` 的主要实测值：

| 场景 | 节点命中/重算 | 与强制全量重算最大差 | 与稠密参考最大差 |
| --- | --- | --- | --- |
| `basic_causal` 基线 | 33 节点全计算 | - | `2.22e-16` |
| `dense_float32` 基线 | 22 节点全计算 | - | `1.11e-16` |
| `partial_tail` 基线 | 50 节点全计算 | - | `2.22e-16` |
| 替换第 0 个 K/V 块 | 重算 17 / 复用 16 | `0.0` | `1.80e-16` |
| 非因果增长追加块 | 重算 22 / 复用 20 | `0.0` | `3.33e-16` |

示例结束后缓存诊断：144 条有效条目、0 条损坏、总大小约 111 KiB。

## 项目结构

```text
flash_attn_cache/
  graph.py        # DAG 构建、拓扑校验、失效后代计算
  compute.py      # 分块打分、softmax、在线合并、finalize
  cache.py        # 内容寻址快照、校验和、版本、原子写入、锁
  engine.py       # 执行器、增量接口、资源控制、自检
  validation.py   # JSONL 与任务/数组校验
  models.py       # 任务、数组、运行时与 softmax 配置
  reference.py    # 稠密 float64 参考实现
  samples.py      # 本地样本加载
scripts/
  generate_samples.py
  run_validation.py
examples/
  run_demo.py
tests/
samples/
artifacts/
```
