# 分块注意力算子组件 — 技术说明

## 1. 组件结构

```
chunked_attn/
  errors.py       结构化业务码（E_*）与 BizError（code/field/case_id/stage/details）
  config.py       StabilityConfig：次正规数保护、舍入模式、容差、显式平局裁决
  core.py         确定性两级分块注意力内核（float64 在线 softmax）
  planner.py      q_block_len / kv_block_len 联合取舍裁决（显式优先级规则）
  ready.py        按 Q 分块逐块就绪查询接口 + 首块耗时观测/估算
  consistency.py  分块拼接 vs 整段输出的逐字节一致性裁决
  stability.py    分块统计量还原整段 softmax 分母校验
  snapshot.py     版本化快照（单调版本号 + SHA-256 校验和 + 整体替换）
  validation.py   输入与配置校验链（结构化业务码 + 阶段归因）
  selfcheck.py    跨模块一致性自检（E_INCONSISTENT）
  api.py          可集成领域组件 ChunkedAttentionOperator（四个入口）
  samples.py      固定种子确定性样本生成（default_rng / PCG64）
  cli.py          命令行入口：generate / run / selfcheck / demo
scripts/gen_samples.py   样本生成脚本
tests/test_chunked_attn.py  25 项端到端测试
```

## 2. 逐字节一致性的构造性保证（业务要求 1、3）

- 打分与 PV 归约采用「广播逐元素乘 + 固定轴 np.sum」，归约顺序只依赖被归约轴
  长度，与 Q 分块方式无关；因此 `q_block_len == seq_len` 的整段 pass 与任意
  Q 分块 pass 在 float64 下逐位一致。
- 整段 pass 与分块 pass 使用**完全相同的 kv_block_len 与升序 KV 合并序列**，
  块边界在线归一化统计量（m、denom、acc）的合并口径因此构造性相同。
- 裁决按字节比较（`tobytes()`），首个偏差按升序 (head, row, col) 索引报告
  （显式声明的同分/同位置裁决规则：`index_ascending`）。

## 3. 首块延迟（业务要求 2）

- 观测：`BlockReadySession` 用 `time.perf_counter` 对本地计算循环实测首块
  可查询耗时。
- 估算：`est = n_kv_blocks * (0.1ms + q_blk*kv_blk*head_dim*2.5e-6ms)`，
  常数由本地样本标定（固定循环开销 + 逐元素成本），随首块 Q 分块长度、
  KV 分块数与每块打分规模记录在报告 `first_block.estimate` 中。
- 超时返回 `E_LATENCY_BUDGET`，`details.completed_blocks` 附带已完成分块清单。

## 4. 联合分块裁决（业务要求 4）

显式优先级（`planner.PRIORITY_RULE`）：
`P1 可行性(est_first_block<=budget) > P2 最小首块耗时估计 > P3 最小驻留内存 >
P4 最少合并次数 > P5 更少分块（升序）`。分块计划记录裁决依据、所选两档分块
与全部候选组合的三轴代价估计（`plan.candidates`）。无可行组合时选择估计首块
耗时最小者并置 `feasible=false`，由运行时以 `E_LATENCY_BUDGET` 呈现证据。

## 5. 数值稳定性（业务要求 5）

- 分块最大值、归一化分母、输出累加全程 float64。
- 次正规数保护可配置（`subnormal_protection`，小于 float64 tiny 即刷零）；
  舍入模式可配置（`nearest_even` / `toward_zero`，作用于输出向 float32 的转换）。
- 还原公式：`denom = Σ_b exp(m_b - m_global) * l_b`，与整段分母比较，
  超出声明容差（默认 1e-12）返回 `E_STABILITY_VIOLATION` 并指出分块位置。

## 6. 快照（业务要求 6）

三类快照（`block_plan` / `norm_params` / `ready_list`）各自携带单调递增版本号
与内容 SHA-256 校验和；提交即原子整体替换（`os.replace`），不做增量修补；
版本不退则 `E_SNAPSHOT_ROLLBACK`，校验和不符或文件损坏则 `E_SNAPSHOT_CORRUPT`。

## 7. 业务码一览

| 代码 | 含义 | 阶段 |
|---|---|---|
| E_MISSING_FIELD / E_DUPLICATE_CASE_ID / E_INVALID_BLOCK / E_INVALID_CAUSAL / E_INVALID_DTYPE | 清单校验 | manifest_validate |
| E_DIM_MISMATCH / E_HEADS_MISMATCH | 数组维度/头数校验 | array_validate |
| E_LATENCY_BUDGET | 首块（或当前块）耗时超预算 | compute |
| E_BLOCK_MISMATCH | 分块拼接与整段输出逐字节不一致 | consistency |
| E_STABILITY_VIOLATION | 分母还原偏差超容差 | stability |
| E_SNAPSHOT_ROLLBACK / E_SNAPSHOT_CORRUPT | 快照回退/损坏 | snapshot |
| E_INCONSISTENT | 跨模块自检不一致 | selfcheck |

## 8. 实际验证结果（本机，Python 3.10.12 + NumPy 1.26.4）

- `python -m pytest tests/ -q`：**25 passed**。
- `python -m chunked_attn.cli demo`：4 个确定性样本全部
  `byte_identical=true`、`deterministic=true`、`within_budget=true`；
  11/11 负向探针命中预期业务码；跨模块自检 `OK`。
- 首块耗时示例（实测 vs 估算）：case-001 0.98ms vs 0.14ms，
  case-004 3.40ms vs 0.43ms（估算为标定模型值，实测含首块预热）。
- 分母还原最大偏差 3.6e-15，远低于声明容差 1e-12。
- 快照版本跨运行单调递增（首次运行 v1，二次运行 v2），回退提交被
  `E_SNAPSHOT_ROLLBACK` 拒绝。

## 9. 复现步骤

```bash
python3 -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install "numpy>=1.24" pytest
python -m chunked_attn.cli demo                 # 生成样本 + 跑流水线 + 探针 + 报告
python -m chunked_attn.cli run --with-probes    # 仅重跑流水线（快照版本递增）
python -m chunked_attn.cli selfcheck            # 仅跨模块自检
python -m pytest tests/ -q                      # 测试套件
```

报告写入 `outputs/report.json`，快照写入 `snapshots/`，样本写入 `samples/`。
全过程本地完成，不依赖 GPU、模型文件或外部服务。
