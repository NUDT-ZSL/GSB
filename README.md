# FlashAttention 分块算子诊断组件

`flashdiag` 是一个可集成、确定性的 FlashAttention 分块诊断组件。它把一次分块注意力拆成四个流水线阶段，使用内容寻址缓存和 SQLite 快照账本，在某个头的 Q/K/V 内容随 `weight_revision` 升级后，只重算依赖该内容的 score tile、归一化统计和输出写回节点。

核心验收路径：

- 增量修订输出与全量分块重算逐元素完全一致。
- 分块局部 `max/denom` 合并后与整段注意力统计在容差内一致，分块输出拼接后与整段输出一致。
- 未受影响的头、Q 分块、score tile、norm、writeback 节点不重算。
- 账本可重放，并校验节点校验和、依赖哈希、快照校验和和导出文件。
- 并发提交只有一个赢家，后到者返回 `E_STAGE_BUSY`。

## 快速开始

环境要求：Python 3.10+、NumPy。实现只使用 Python 标准库和 NumPy，不需要 Docker。

```bash
python3 -m pip install -e .
python3 -m flashdiag demo
```

`demo` 会执行以下完整示例：

1. 用固定种子脚本生成 `data/tasks.jsonl`、`data/tasks_rev.jsonl` 和 Q/K/V `.npy` 文件。
2. 清空并重建 `artifacts/flashdiag-demo` 运行时目录。
3. 写入 base 快照。
4. 写入更高 `weight_revision` 的增量快照。
5. 查询每个 case 的账本历史、重算/复用清单、资源状态和业务码。
6. 从账本重放并执行整段对照与一致性检查。

不安装包时也可直接运行：

```bash
PYTHONPATH=. python3 -m flashdiag demo
```

单独运行固定种子生成器：

```bash
PYTHONPATH=. python3 scripts/generate_samples.py --data-dir data
```

## 输入格式

任务清单是 JSONL。每行必须包含：

```json
{
  "case_id": "causal-q16-kv16",
  "seq_len": 64,
  "head_dim": 16,
  "num_heads": 2,
  "q_block_len": 16,
  "kv_block_len": 16,
  "weight_revision": 1,
  "dtype": "float64",
  "causal": true,
  "seed": 101,
  "q_path": "arrays/causal-q16-kv16/rev1/q.npy",
  "k_path": "arrays/causal-q16-kv16/rev1/k.npy",
  "v_path": "arrays/causal-q16-kv16/rev1/v.npy"
}
```

`q_path`、`k_path`、`v_path` 可选，相对 JSONL 文件所在目录解析。省略时按 `arrays/<case_id>/rev<N>/{q,k,v}.npy`、`<case_id>/rev<N>/{q,k,v}.npy` 等本地约定查找。数组形状必须是 `(num_heads, seq_len, head_dim)`，文件 dtype 必须与任务声明一致，且所有元素有限。

同一 JSONL 内重复 `case_id` 返回 `E_INPUT_INVALID`。同一 case 的更高修订必须保持形状、块长、dtype、causal、seed 等配置不变；同一 `case_id + weight_revision` 的相同内容可幂等复用，内容或配置不同返回 `E_INPUT_INVALID`，修订回退返回 `E_SNAPSHOT_ROLLBACK`。

## 组件结构

```text
flashdiag/
  planner.py   输入校验、head/block 哈希、Q/KV 分块计划
  core.py      float64 online-softmax、分块合并、整段对照
  store.py     内容寻址的四阶段节点产物存储
  ledger.py    SQLite 快照账本、原子激活、并发提交控制
  engine.py    增量失效、缓存复用、资源预算、自检
  cli.py       JSON 命令行、完整 demo
scripts/
  generate_samples.py
tests/
  test_flashdiag.py
```

流水线阶段：

| 阶段 | 落盘节点 | 说明 |
| --- | --- | --- |
| 输入校验与计划 | `validation`, `plan` | 阶段 1 产生两个产物；validation 固化 Q/K/V 输入，plan 按 head/Q block 写入 `block_plans` 表 |
| 分块打分 | `score:hX:qY:kZ` | 每个 head、Q block、KV block 一个 tile，保存 score、局部 `max`、局部指数和、局部输出分子 |
| 在线归一化 | `norm:hX:qY` | 按 KV block 顺序合并局部统计，保存合并 `max/denom/acc` |
| 输出写回 | `writeback:hX:qY` | 由合并分母归一化并保存 Q block 输出 |

所有 `.npy` 和 JSON 元数据保存在 artifact store 中。SQLite 只保存账本、节点描述、校验和、依赖哈希和计划记录。

## 增量修订

修订运行时：

1. 比较旧验证产物中的 Q/K/V head 哈希，确定实际变化的 head。
2. 比较每个 KV block 的 K/V 哈希，并按当前 causal 计划找到可见的 Q block。
3. score 缓存键包含相关 Q/K/V block 哈希，因此只有依赖变化块的 tile 会 miss。
4. norm 键包含该 Q block 所有 score 产物哈希；writeback 键包含 norm 哈希。
5. 未命中的节点才计算。命中节点直接读取并校验磁盘快照。
6. 报告 `changed_heads`、`affected_q_blocks`、`recomputed`、`reused`。

validation/plan 是全局依赖节点；新内容会生成新的 validation 键。plan 的语义内容哈希不含自身缓存键，因此结构未变时，plan 内容哈希保持稳定，不会连带使所有 score tile 失效。

全量重算审计在内存中执行，不写入缓存节点，也不替换已提交的分块结果。它用于证明增量输出与全量分块重算逐元素完全一致。

## 数值模型

每个 score tile 计算：

```text
S = Q_block @ K_block.T / sqrt(head_dim)
S = causal_mask(S)
m_block[r] = max_j S[r, j]
l_block[r] = sum_j exp(S[r, j] - m_block[r])
p_block[r] = sum_j exp(S[r, j] - m_block[r]) * V_block[j]
```

在线合并：

```text
m_new = max(m_old, m_block)
alpha = exp(m_old - m_new)
beta  = exp(m_block - m_new)
l_new = l_old * alpha + l_block * beta
p_new = p_old * alpha + p_block * beta
```

最终输出为 `p_new / max(l_new, float64_tiny)`。所有 score、指数、累加器、局部统计和输出均为 `float64`。指数下限使用 `log(float64_tiny)`，避免次正规数进入后续累加；causal mask 位置保持精确零贡献。

默认审计容差为 `atol=1e-9, rtol=1e-9`。增量输出对全量分块重算使用零容差逐元素比较；超过容差返回 `E_TOLERANCE_EXCEEDED`，并给出首个差异的路径、索引、实际值、期望值和绝对误差。

## SQLite 账本

主要表：

- `snapshots`：case、单调版本、weight revision、输入/输出/快照/依赖校验和、时间戳、活动标记、前驱快照。
- `ledger_nodes`：节点类型、版本、缓存键、内容校验和、依赖校验和、artifact manifest 路径。
- `block_plans`：每个 head/Q block 的 plan 记录、KV block 范围、计划校验和、快照版本。

提交使用 `BEGIN IMMEDIATE`。新快照先以完整节点集合写入，再在同一事务中停用旧活动快照并激活新快照；旧快照保留为历史记录。不会对已提交节点做增量修补。版本或校验和冲突不会原地覆盖，后到并发写入返回 `E_STAGE_BUSY`。

账本重放会重新计算缓存键链、读取并校验所有 artifact、从 `validation` 节点读取已固化的 Q/K/V 输入、重建 norm 和 writeback、重新拼接输出，并执行全量分块与整段对照。即使原始输入文件后来被覆盖，已提交版本仍可重放。

## 资源预算

`ResourcePolicy` 提供两个上限：

- `max_resident_bytes`：单次调用估算的 float64 分块驻留内存上限。
- `max_parallel_q_blocks`：可并行重算的 Q block 数上限。

如果待并行 Q block 数超过上限，默认降级为串行并设置 `resource.degraded=true` 和原因；设置 `allow_serial_degrade=false` 时返回 `E_RESOURCE_LIMIT`。即使单个 Q block 已超过内存上限，也返回 `E_RESOURCE_LIMIT`。

## 业务码

| 业务码 | 场景 |
| --- | --- |
| `OK` | 运行、查询或重放成功 |
| `E_INPUT_INVALID` | 清单、维度、dtype、causal 声明、重复 case/修订冲突 |
| `E_STAGE_BUSY` | 并发快照提交冲突或活动快照在构建期间变化 |
| `E_RESOURCE_LIMIT` | 内存或并行预算拒绝执行 |
| `E_TOLERANCE_EXCEEDED` | 数值对照超出容差 |
| `E_SNAPSHOT_ROLLBACK` | 修订或版本回退 |
| `E_SNAPSHOT_MISSING` | 请求的快照不存在 |
| `E_LEDGER_INTEGRITY` | 账本、依赖、输入或输出校验和无法重放 |
| `E_CACHE_CORRUPT` | artifact 内容、manifest 或数组哈希损坏 |

## Python 集成接口

```python
from flashdiag import FlashAttentionDiagnostics, ResourcePolicy

engine = FlashAttentionDiagnostics(
    "artifacts/runtime/ledger.db",
    "artifacts/runtime/store",
    policy=ResourcePolicy(
        max_resident_bytes=256 * 1024 * 1024,
        max_parallel_q_blocks=4,
    ),
    export_root="artifacts/runtime/exports",
)

base_results = engine.run_manifest("data/tasks.jsonl")
revision_results = engine.run_manifest("data/tasks_rev.jsonl")
verification = engine.verify_case("causal-q16-kv16")
snapshot = engine.query_case("causal-q16-kv16")
history = engine.history("causal-q16-kv16")
```

## 常用命令

```bash
# 生成固定种子样本
python3 -m flashdiag generate --data-dir data

# 初始化账本
python3 -m flashdiag init --ledger artifacts/flashdiag/ledger.db

# 运行 base 与 revision
python3 -m flashdiag run \
  --manifest data/tasks.jsonl \
  --manifest data/tasks_rev.jsonl \
  --ledger artifacts/flashdiag/ledger.db \
  --store artifacts/flashdiag/store \
  --export-root artifacts/flashdiag/exports

# 查询 active snapshot
python3 -m flashdiag query --case-id causal-q16-kv16 \
  --ledger artifacts/flashdiag/ledger.db \
  --store artifacts/flashdiag/store

# 从账本重放
python3 -m flashdiag verify --case-id causal-q16-kv16 \
  --ledger artifacts/flashdiag/ledger.db \
  --store artifacts/flashdiag/store

# 运行全部测试
python3 -m unittest discover -v
```

更细的缓存键、账本事务和验证流程见 `docs/design.md`；实际验证记录见 `docs/validation.md`。
