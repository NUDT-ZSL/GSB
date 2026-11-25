# Deterministic Sparse MoE Routing

这是一个从零实现、可集成的稀疏混合专家（MoE）路由链路示例。它使用固定种子在本地生成 token、门控权重和专家权重，不依赖真实模型、网络或容器环境。

组件覆盖完整多级流水：

`输入解析/门控打分 → 块内容量分配 → 分块专家计算 → 加权归约 → 一致性裁决 → 缓存/失效/快照`

核心保证是：在相同的块内容量语义下，逐块就绪路由与一次性全序列参考计算产生逐 token 完全一致的 float64 结果。

## 快速开始

环境：Linux/WSL Ubuntu、Python 3.10 或 3.11、NumPy。

```bash
python3 -m pip install -r requirements.txt
python3 scripts/generate_samples.py
python3 -m pytest -q
python3 -m moe_router demo \
  --cases samples/cases.jsonl \
  --workdir artifacts/demo \
  --out artifacts/validation_report.json \
  --reset
```

也可以直接运行路由审计：

```bash
python3 -m moe_router route \
  --cases samples/cases.jsonl \
  --cache artifacts/cache \
  --out artifacts/route_report.json
```

## 代码结构

- `moe_router/validation.py`：JSONL、case、dimension、revision 和资源上限校验。
- `moe_router/weights.py`：固定种子 token、门控权重、专家权重与 manifest 生成。
- `moe_router/routing.py`：门控、块内容量、专家执行、逐块查询和一致性裁决。
- `moe_router/cache.py`：四类产物的内容寻址缓存、依赖键和过期拒绝。
- `moe_router/invalidation.py`：专家/策略修订后的定向失效、复用与后缀重算。
- `moe_router/snapshots.py`：整体快照提交、校验和、非阻塞并发锁。
- `moe_router/demo.py`、`moe_router/cli.py`：完整端到端示例。
- `samples/cases.jsonl`：固定种子任务清单；`samples/weight_manifest.json`：权重摘要。
- `tests/`：28 项自动化测试。
- `docs/DESIGN.md`：确定性语义、缓存布局、错误码与失效策略说明。

## 最小集成示例

```python
from moe_router import ContentAddressedCache, RoutingSession, build_weight_bundle
from moe_router.validation import load_cases

case = load_cases("samples/cases.jsonl")[0]
bundle = build_weight_bundle(case)
session = RoutingSession(bundle, ContentAddressedCache("artifacts/runtime-cache"))

session.compute_next_block()
first = session.query_block(0)  # 此时后续块尚未计算
print(first["first_block_query_ms"])
print(first["expert_revisions_used"])
print(first["capacity_summary"])

session.route_all()
audit = session.consistency_report()  # 超差时抛出 E_BLOCK_MISMATCH
print(audit["max_abs_diff_output"], audit["first_diff_token"])
```

## 实际验证结果

本机运行固定种子样本 `20261126` 的 6 个 case，结果记录在 `artifacts/validation_report.json`：

- 分块路由与一次性全序列参考：6/6 case 一致。
- 门控分布、专家加权贡献、最终路由输出的最大绝对偏差均为 `0.0`。
- 首个差异 token：全部为 `null`。
- 6 个 case 均产生容量溢出记录，块间容量没有互相借用。
- 缓存对象完整性检查：99 个对象，0 个损坏。
- 定向失效示例：`moe-case-04` 仅专家 2 修订到 revision 2；复用块 `[0]`，重算后缀块 `[1, 2, 3]`，重算后的全量一致性偏差为 `0.0`。
- 专家修订后用新依赖读取旧块索引返回 `E_STALE_RESULT`，错误携带 `case_id=moe-case-04` 和 `block_index=0`。
- 快照：v1 被 v2 整体替换；快照校验和分别为
  `66f8bb6b...` 和 `e1b67cdf...`；旧版本提交返回 `E_SNAPSHOT_ROLLBACK`，当前版本保持为 2。
- 自动化测试：`28 passed`。

首块查询耗时是进程内 `monotonic_ns()` 的实际计时，受机器负载影响；本次 6 个 case 约为 41–46 ms。该指标用于可查询性观测，不作为稳定性基准。

## 关键语义

- 门控：对全部专家计算稳定 softmax；按 `(-score, expert_id)` 排序取 top-k，并仅对选中的 top-k 重新归一化。
- 容量：每个块独立计算
  `capacity = max(1, ceil(capacity_factor * block_token_len / num_experts))`，
  不允许跨块借用余量。
- 溢出：每个专家按 `(-gate_score, token_position, top_k_rank)` 截断，并记录全局 token 位置、专家、rank 和门控分数。
- 专家算术：统一使用逐 token、逐输出维度的确定性点积；专家按 `expert_id` 升序计算，最终坐标使用 `math.fsum` 累加。
- 全量参考：使用同样的块内容量边界，但在一次不中断的全序列 pass 中执行专家计算，便于逐 token 审计。
- 缓存键：包含 token 内容哈希、门控权重摘要、专家修订映射、专家库摘要、`router_revision`、`top_k`、`capacity_factor`、`block_size`、块索引和 token 区间。

详细设计、错误码和文件布局见 `docs/DESIGN.md`。

