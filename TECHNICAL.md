# LLM 推理采样与解码策略本地诊断工作台 — 技术说明

## 环境

- Python 3.11.18（`.venv`，由 `uv python install 3.11` 安装）+ NumPy 2.4.6
- 仅标准库 + NumPy；无 Docker、无外部服务、无真实模型
- 样本由固定主种子 `20261124` 生成：`python -m llm_workbench.generate_samples --out data`

## 架构与流水线

```
ingest/validate -> admit(预算裁决+仲裁) -> execute(进程池+检查点) -> snapshot -> selfcheck
```

| 模块 | 职责 |
| --- | --- |
| `llm_workbench/sampling.py` | 确定性逐 step 采样核心（温度缩放、top-k/top-p 截断、种子驱动抽取） |
| `llm_workbench/validation.py` | 输入与配置校验链，错误归因到 stage/field/request_id |
| `llm_workbench/scheduler.py` | 三重预算准入裁决 + 显式取舍仲裁 + 确定性分块 |
| `llm_workbench/executor.py` | 单进程多 worker 进程池执行、请求边界检查点、强制终止恢复 |
| `llm_workbench/snapshots.py` | 三类快照的版本化整体替换、校验和验证 |
| `llm_workbench/selfcheck.py` | 独立复算的跨模块一致性自检 |
| `llm_workbench/pipeline.py` | 端到端编排 |
| `llm_workbench/workbench.py` | 交互式诊断工作台（REPL）与报告导出 |
| `llm_workbench/generate_samples.py` | 确定性样本生成（tasks.jsonl / vocab.json / config.json） |

## 确定性设计（要求 1、2）

- 每个请求的两条随机流显式声明：`SeedSequence([seed, LOGITS_STREAM])` 产生逐 step logits，
  `SeedSequence([seed, DRAW_STREAM])` 产生逐 step 均匀抽取；不依赖系统时间、全局随机状态。
- 全部 float64 固定运算顺序；同分候选用 `np.lexsort((token_ids, -probs))` 按 token 编号升序打破平局。
- top-p 保留最小前缀使累计质量 ≥ p，且至少保留 1 个候选。
- 每个请求的结果是 (seed, step) 的纯函数，与批次组成无关；执行器在收尾时把每个批次结果与
  主进程单独复算逐字节比对，不一致抛 `E_BATCH_INCONSISTENT`（含 request_id 与首个偏差 step）。

## 预算与仲裁（要求 3、4）

- 三重预算（均为显式声明的估算量，非墙钟派生）：
  - `step_budget`：总采样步数 = Σ effective target_len
  - `kv_budget`：单波次驻留 KV 条目 = Σ(prompt_tokens + target_len)
  - 槽位预算：`max_slots`（每波并发槽）× `max_waves`（最大波次）
- 准入按 (-priority, request_id) 贪心，块边界/派发/归并顺序确定性可复现；
  超限分别返回 `E_STEP_BUDGET` / `E_KV_BUDGET` / `E_SLOT_BUDGET` 并记录各预算余量；
  被拒请求不出现在任何采样摘要中。
- 仲裁由 `tradeoff_priority`（progress/memory/quality 的排列）驱动，全部记录到调度报告：
  - `topp_tightened`：memory 优先于 quality 时把 top_p 收紧到 `topp_floor`
  - `len_truncated`：progress 优先于 quality 时截断 target_len 以适配步数/KV 预算
  - `slot_reduced`：memory 优先于 progress 时放弃空闲槽位、推迟到新波次
  - `topp_kept`：quality 优先时保留宽 top-p 并记录依据

## 快照（要求 5）

- 三类快照 `artifacts` / `ledger` / `decisions`，各自单调递增版本号 + SHA-256 校验和。
- 版本或校验和变化即原子整体替换（tmp 写入 + `os.replace`），不做增量修补。
- 旧版本覆盖尝试 → `E_SNAPSHOT_ROLLBACK`；损坏/校验和不符 → `E_SNAPSHOT_CORRUPT`（含类型与版本号）。

## 检查点与恢复（要求 6）

- 执行器在每个请求边界原子提交 `checkpoint.json`（已提交 id、完整结果、已消耗预算、下一序号、
  执行顺序指纹）。
- 重启读取最近已提交检查点，从下一请求继续；已提交结果与预算不重算、不重复计入；
  未提交中间状态丢弃并从该请求起点重建。
- 只承诺基于已提交检查点的恢复，不承诺强制终止时生成退出报告。

## 错误码（要求 7、8）

`E_DUP_REQUEST`、`E_SAMPLING_INVALID`、`E_LENGTH_INVALID`、`E_SEED_INVALID`、`E_VOCAB_MISSING`、
`E_STEP_BUDGET`、`E_KV_BUDGET`、`E_SLOT_BUDGET`、`E_BATCH_INCONSISTENT`、
`E_SNAPSHOT_ROLLBACK`、`E_SNAPSHOT_CORRUPT`、`E_INCONSISTENT`。
每个错误携带 stage / field / request_id 归因；单请求失败被隔离记录，不污染其他请求。

## 自检（要求 8）

`selfcheck` 用 (params, seed) 独立复算每个准入请求并逐字节比对；核对各阶段 request_id 集合
（input = valid + invalid，valid = admitted + rejected，executed = admitted，downgraded ⊆
admitted ∪ rejected）；校验被拒/被降级记录与实际执行一致、账本步数平账。不一致返回 `E_INCONSISTENT`。

## 使用

```bash
uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python numpy
.venv/bin/python -m llm_workbench.generate_samples --out data
.venv/bin/python -m llm_workbench.workbench --data data --work work          # 交互式 REPL
.venv/bin/python -m llm_workbench.workbench --data data --work work \
    --report artifacts/diagnostic_report.json                                # 非交互报告
.venv/bin/python -m llm_workbench.workbench --data data --work work_crash \
    --crash-after 5 --report artifacts/diagnostic_report_recovery.json       # 强制终止恢复
.venv/bin/python -m unittest discover -s tests -v                            # 测试
```

## 实际验证结果（本地确定性样本，24 请求 / 词表 256）

- `python -m unittest discover -s tests`：**43 个测试全部通过**（确定性、平局打破、截断、
  三重预算拒绝、仲裁、快照回滚/损坏、崩溃恢复、校验归因、端到端自检、篡改检测）。
- 并行 vs 单请求：执行器在 4 worker 进程池下逐字节比对通过；串行模式与进程池模式结果一致。
- 预算裁决：23 准入 / 1 拒绝（`E_STEP_BUDGET`，步数预算 720 用尽）/ 7 降级
  （6 次 `topp_tightened`、1 次 `len_truncated`），账本 steps_reserved=720、kv_peak=434、waves=6。
- 强制终止恢复：`--crash-after 5` 后从检查点序号 5 恢复，已提交 5 条不重算，
  恢复后完整结果与不间断运行**逐字节一致**（`clean-vs-recovered byte-identical: True`）。
- 吞吐/内存指标与本地样本规模相容：720 步 / 0.48s ≈ 1499 steps/s（4 worker 单进程池），
  驻留 KV 峰值 153 条目。
- 诊断报告：`artifacts/diagnostic_report.json`、`artifacts/diagnostic_report_recovery.json`。
