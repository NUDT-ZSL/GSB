# RoPE 分块注意力算子组件 — 技术说明与验证记录

## 1. 设计总览

纯 NumPy、零网络依赖、无容器。四个解耦层：

| 层 | 模块 | 职责 |
| --- | --- | --- |
| 配置校验 | `config.py` | JSONL 任务解析、字段/取值校验、资源与数值策略配置 |
| 算子核心 | `rope.py` `attention.py` `numeric.py` `sampling.py` | 确定性采样、RoPE、分块在线 softmax、整段参考 |
| 状态与缓存 | `cache.py` `state.py` `nodes.py` | 内容寻址缓存、DAG 节点、原子快照状态机 |
| 执行与示例 | `engine.py` `pipeline.py` `invalidate.py` `selfcheck.py` `demo.py` | 进程池流水线、恢复、增量失效、自检与 CLI |

数据布局统一为 `(seq_len, num_heads, head_dim)`。

## 2. RoPE 口径（业务要求 1）

* 要求 `head_dim` 为正偶数，半维 `half = head_dim // 2`；维度 `i` 与
  `i + half` 配对（GPT-NeoX 半维配对），奇数维在 `validate_task_dict`
  直接返回 `E_ODD_HEAD_DIM`。
* 角频率 `inv_freq_i = theta_eff ** (-2i / head_dim)`；
  位置角 `angle[p, i] = scaled_position[p] * inv_freq_i`。
* 变体：
  * `standard`：`theta_eff = base_theta`，位置乘 `pos_scale`；
  * `linear`：线性位置内插，位置乘 `pos_scale`；
  * `ntk`：`theta_eff = base_theta * pos_scale ** (d/(d-2))`。
* 旋转：
  `x'_i = x_i cos - x_{i+h} sin`，
  `x'_{i+h} = x_{i+h} cos + x_i sin`。
* Q/K/V 由 `np.random.SeedSequence([seed, layer, role, kv_revision])`
  派生的独立 `default_rng` 生成；同 case/seed/修订号逐字节一致
  （测试 `test_generation_byte_identical` 与跨进程 blob 哈希比对）。

## 3. 分块在线 softmax（业务要求 1/6）

* 打分 `Q @ K^T / sqrt(head_dim)`，causal 下键位置 `k > p` 置 `-inf`。
* 每块保存 `block_max` 与块内平移指数和 `block_denom`，合并公式：
  `M = max_b m_b`，`D = sum_b d_b exp(m_b - M)`。
* 统计量全程 float64 累加；`stat_dtype=float32` 时按四种舍入模式
  （`half_to_even / truncate / down / half_up`）量化，
  次正规数保护把低于 `finfo.tiny` 的值冲零。
* causal 整块被掩码时保留 `-inf` 最大值，`exp(-inf)=0` 自然归零，
  保证分母仍可还原。
* 整段参考路径不分块，输出逐元素最大绝对/相对偏差；分母还原相对偏差
  超过 `tolerance`（默认 1e-12）返回 `E_NUMERIC_UNSTABLE`，带
  case_id、层号与块序号。实测误差约 1e-16。

## 4. 阶段状态机与原子快照（业务要求 2/7）

四阶段固定顺序 `angles -> rotate -> score -> writeback`。

* 阶段对全部层 × 全部 batch 完成后才提交；阶段内 batch 进度只写
  `progress.json` 侧车，绝不进入已提交快照。
* 快照 `snapshots/<version>.json` 先写同目录 `.tmp` 再 `os.replace`
  原子落地；含单调版本号、已完成键集合与 SHA-256 校验和。
* 版本号与阶段名一一对应（`version % 4` 映射），旧版本提交返回
  `E_SNAPSHOT_ROLLBACK`。
* 提交锁为非阻塞 `flock`/`msvcrt` try-lock，冲突立即 `E_STAGE_BUSY`。

## 5. 强制终止与恢复（业务要求 3）

* `--kill-after-stage N`：提交 N 个阶段后对自身发 `SIGTERM`
  （Windows 等价 `TerminateProcess`），无 Python 清理。
* 重启读取 `head.json` 指向的最近快照，从下一阶段继续；
  `resume_mode=batch`（默认）结合进度侧车做 batch 粒度续跑，
  `strict_stage` 丢弃侧车从阶段起点重建。
* 已提交节点经内容寻址缓存全部命中、零重算；未提交中间产物随缓存
  键不匹配自然作废。输出摘要基于缓存 output 节点 blob 哈希统一计算，
  恢复运行与不间断运行逐字节一致（见验证记录）。
* `--interrupt-after-stage N` 在提交边界置中断标记，优雅落盘并输出
  `exit_report`，业务码 `E_INTERRUPTED_GRACEFUL`。

## 6. 内容寻址缓存与键构造（业务要求 4）

* `blobs/<sha256>.npz` 存数组，`manifest.json` 以 key_hash 为主键
  存条目；写入均为临时文件 + 原子替换。
* 缓存键纳入：节点全部输入摘要、上游产物哈希、`base_theta`、
  `pos_scale`、变体、`block_size`、`causal`、`kv_revision`、dtype。
* `ContentCache.inspect_node(case_id, block)` 返回键组成、key_hash 与
  blob 哈希；`verify_entry` 重算数组内容哈希核对复用真实性。

## 7. 增量失效传播（业务要求 5）

* K/V 内容变化（`kv_revision` 升高、block_size 不变）：失效
  `kv_k/kv_v -> rope_k -> score_* -> stats_* -> output/ref`，
  复用 `q/angles/rope_q`。
* block_size 变化（修订号不变）：块边界与全部下游失效，RoPE 上游
  全部复用。
* 执行前校验复用节点在缓存中真实存在、重算节点不得意外命中；
  重算/复用无交集且并集覆盖该 case 全部节点，违反返回
  `E_INCONSISTENT`。

## 8. 资源预算（业务要求 7）

`RunConfig` 提供 `max_block_bytes / max_cache_entries /
max_parallel_nodes` 与 `resource_policy`：

* `error`：超限即 `E_RESOURCE_LIMIT`；
* `degrade`：降级到受限并发并在 `resource_degradations` 标记类别。

进程池使用 `multiprocessing.get_context("spawn")`，Windows/Linux
行为一致；worker 顶层函数 `run_worker` 捕获异常并以业务码 + 阶段 +
层号 + 块序号回传主进程。

## 9. 业务码清单

| 码 | 触发场景 |
| --- | --- |
| `E_DUPLICATE_CASE` | 重复 case_id |
| `E_DIM_MISMATCH` | seq_len 与数组维度不符 |
| `E_ODD_HEAD_DIM` | head_dim 奇数导致配对越界 |
| `E_VALIDATION` | base_theta 越界、pos_scale 非正、block_size 非法等 |
| `E_REVISION_ROLLBACK` | kv_revision 回退 |
| `E_UNSUPPORTED_DTYPE` | dtype 不在 float32/float64 |
| `E_STAGE_BUSY` | 提交锁非阻塞冲突 |
| `E_SNAPSHOT_ROLLBACK` | 版本回退或阶段/版本映射错误 |
| `E_RESOURCE_LIMIT` | 资源预算超限 |
| `E_NUMERIC_UNSTABLE` | 分母还原偏差超容差 |
| `E_INCONSISTENT` | 重算/复用分区或缓存复用不成立 |
| `E_WORKER` | worker 内未知异常包装（保留归因） |

## 10. 验证记录（本机实际执行）

环境：Python 3.10.12、NumPy 1.26.4、Linux WSL2（代码按 Windows 兼容
编写，文件锁与进程池均有平台分支）。

* `python -m unittest discover -s tests`：37 个用例全部通过，覆盖
  RoPE 确定性、半维配对、causal、四种舍入、校验业务码、缓存键核对、
  两类失效传播、快照原子性、非阻塞锁、回退保护、资源预算、
  worker 归因、自检、子进程强杀恢复与 KeyboardInterrupt。
* 强杀恢复：提交 2 个阶段后 SIGTERM（返回码 -15），快照目录恰好
  保留 1/2 两个版本；重启后 `angles/rotate` 完成计数为 0（零重算），
  `output_digest` 与不间断运行完全一致。
* 跨进程逐字节：两个独立缓存目录同任务产出 72 个共同节点，blob 哈希
  零差异，`rope_q` 数组 `tobytes()` 完全相同。
* 数值：float32/float64 任务分母还原最大相对误差 ~1e-16，分块与整段
  输出最大绝对偏差为 0 或 ~2e-16。

## 11. 运行方式

```bash
python -m pip install --upgrade pip
python -m pip install numpy
python -m rope_attn.generate_tasks --out tasks.jsonl
python -m rope_attn.demo --tasks tasks.jsonl \
    --cache .cache --state .state --out report.json
```

报告 JSON 含逐阶段完成计数、快照版本、缓存命中/未命中、偏差明细、
分母还原、失效报告、缓存键查询与四项自检结果。
