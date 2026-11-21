# 多路召回融合系统技术说明

面向搜索/推荐前链路的单机四路召回 + 加权融合实验框架。全部数据由固定
种子确定性生成，不依赖任何外部服务、账号或网络资源。

## 1. 目录结构

```
recall_fusion/
  config.py       # 配置 dataclass、显式默认值、JSON 文件 + RF_* 环境变量覆盖、合法性校验
  errors.py       # 结构化业务码（E_CONFIG_INVALID / E_RECALL_BUDGET / ...）
  data.py         # 确定性样本生成、倒排/前缀/类别/向量索引、可 pickle 的 DataStore
  recall.py       # 四路确定性召回，统一 Candidate / ChannelOutput 结构
  fusion.py       # 归一化、跨路对齐去重、加权融合、两段式精排
  budget.py       # 双重预算账本、准入裁决、降级路径、三类冲突的策略记录
  memory.py       # 驻留内存账本、FIFO 分片淘汰、资源账本留痕
  snapshots.py    # 版本号 + 校验和、整体替换、回退/损坏检测
  recall_stats.py # 每路统计量与三类快照载荷构造
  worker.py       # spawn 进程池初始化与单查询执行，异常结构化回传
  engine.py       # 分块派发、归并、预算/内存联动、产物与汇总
  cli.py          # run / verify / snapshot-demo 三个子命令
tests/test_pipeline.py  # 19 个 unittest 用例
run_demo.sh              # 一键复现脚本
```

## 2. 确定性可复现保证

* 文档/查询结构：`random.Random(seed)`，向量：`numpy.random.default_rng`，
  每个 doc/query 的向量种子由 `md5(base_seed|tag|id)` 派生，worker 进程内
  无需传递 RNG 状态即可逐字节复现同一向量。
* 所有打分并列均以升序 `doc_id` 兜底排序（doc_id 等宽零填充，字典序等于
  数值序），因此同输入、同配置、同精度标记下候选集合与顺序稳定。
* 进程池统一使用 `spawn` 起点（Linux/macOS/Windows 均支持）；查询按
  query_id 顺序做定长切片，批次按序派发，结果按 query_id 顺序重组，
  worker 完成先后不影响输出。
* 落盘 JSON 一律 `sort_keys=True`，并额外输出
  `results.canonical.jsonl`（紧凑分隔符），用 SHA-256 做逐字节比对。
* 浮点归约顺序固定（按固定路顺序、固定候选顺序），支持 `fp64/fp32`
  两种精度标记，精度标记进入快照与配置文件。

## 3. 四路召回（要求 1）

| 路 | 打分口径 | 过滤 |
| --- | --- | --- |
| `inverted` | 查询词命中文档的词频累加 | category / min_quality |
| `prefix` | 词项前缀展开后词频累加（bisect 查有序词表） | 同上 |
| `category` | quality_score 降序（有 category 过滤时只在该类内排） | 同上 |
| `vector` | 单位化文档/查询向量余弦相似度 | 同上 |

每路输出 `doc_id/raw_score/rank`、`requested_limit`、`matched_total`、
`truncated`（命中数超过实际限额）与 `empty_reason`（`no_hits/limit_zero/
disabled`）。禁用路与未命中路都显式记录，不允许缺省。

## 4. 对齐去重与融合（要求 2、3）

* 按 doc_id 归并四路结果，最终候选集保证无重复 doc_id。
* 归一化两种口径：
  * `minmax`：路内最小-最大缩放，单候选或极差为 0 时记 1.0；
  * `rank_inverse`：`1/rank`。
  全局口径可被单路 `normalization` 覆盖；非法口径返回
  `E_CONFIG_INVALID` 并指明字段。
* 去重策略：
  * `highest_normalized`：同一 doc 的所有命中路都参与加权，主路取归一化
    分最高者（完全并列时按固定路顺序取最先者）；
  * `first_hit`：只有固定路顺序下首条命中路贡献分数。
* 每个候选的 `details` 逐路记录 `raw_score/norm_score/weight/contribution`，
    `fused_score == sum(contribution)` 可由明细独立重算；两段式精排头部队列
    额外保留 `coarse_norm_score/coarse_contribution`。
* 权重为负返回 `E_CONFIG_INVALID`，字段定位到 `channels.<name>.weight`。

## 5. 并行调度与异常隔离（要求 4）

`workers` 与 `batch_size` 均可由配置或 `RF_WORKERS/RF_BATCH_SIZE` 覆盖。
worker 内任何异常都被捕获并转成
`{code,message,stage,field,query_id,traceback}` 结构回传，单查询失败只
记录在 `cache/failures.jsonl`，不影响同批其他查询。

## 6. 双预算准入与降级（要求 5）

* 总候选预算：所有路 `effective_limit` 之和；融合比较预算：对齐后去重
  文档数，两段式时再加上精排范围（`coarse_k`）。
* 准入按 query_id 顺序串行裁决，保证账本确定。放不下时按
  `degrade_priority`（优先级最高者最先被砍）对半缩减该路限额，每一步都
  写入 `plan.degradation_steps`；所有路降到 0 仍放不下则拒绝：
  预算原因 `E_RECALL_BUDGET`，单查询最小内存仍超限 `E_RESOURCE_LIMIT`。
* 被拒查询进入 `cache/rejected.jsonl`，并在 `results.jsonl` 中以显式
  拒绝记录出现（四路空结果 + `empty_reason=query_rejected`），不静默丢结果。

## 7. 驻留内存与淘汰（要求 6）

内存账本使用平台无关的固定字节权重（候选行 96B、融合行 128B、明细单元
48B，见 `budget.BYTES_PER_*`），保证 Linux/macOS/Windows 口径一致。
超限时按 FIFO 把最早归并的完整查询写入编号分片
`shards/shard_00000.jsonl`，内存中只保留聚合计数；所有 retain/evict/
drain 事件实时追加到 `cache/memory_ledger.jsonl`。最终统计量来自聚合
计数器，淘汰不丢失任何计数。

## 8. 精度/内存/延迟冲突裁决（要求 7）

裁决顺序由 `policy_priority`（memory/budget/quality/latency 的排列）
声明，记录在每个查询的 `plan.policy_path` 与 `cache/parallelism.json`：

1. 扩候选 vs 内存：单查询估算字节超限时优先进入降级循环；
2. 两段式粗排+精排 vs 融合比较预算：精排增量比较数放不下时回退单段，
   并记录 `two_stage:rejected_compare_budget_single_stage_fallback`；
3. 并行度 vs 内存/延迟：memory 优先于 latency 且单 worker 公平内存份额
   过小（<8KiB）时把 worker 数减半，理由写入并行度事件。

## 9. 版本化快照（要求 8）

三类快照 `recall_stats/norm_params/fusion_weights` 各自携带单调递增版本号
与 SHA-256 内容校验和。提交时先写不可变版本文件，再原子改写 manifest
指针，属于整体替换而非增量修补：

* 提交版本 <= 当前版本：`E_SNAPSHOT_ROLLBACK`；
* 文件缺失、JSON 损坏、校验和不符、头部与 manifest 不一致：
  `E_SNAPSHOT_CORRUPT`，错误字段给出快照名与版本号。

`python -m recall_fusion.cli snapshot-demo` 会真实演示这两条路径。

## 10. 配置与环境变量

所有参数都有显式默认值（见 `config.DEFAULT_CONFIG`），零参数即可复现。
常用覆盖：`RF_WORKERS`、`RF_BATCH_SIZE`、`RF_TOTAL_CANDIDATE_BUDGET`、
`RF_FUSION_COMPARE_BUDGET`、`RF_MEMORY_LIMIT_BYTES`、`RF_PRECISION`、
`RF_NORMALIZATION`、`RF_DEDUP_POLICY`、`RF_SEED`、`RF_NUM_DOCS`、
`RF_NUM_QUERIES`、`RF_TWO_STAGE`、`RF_DEGRADE_PRIORITY`、
`RF_POLICY_PRIORITY`。

## 11. Windows 兼容说明

仅使用标准库 `multiprocessing` 的 spawn 模式与 `pathlib` 路径；所有入口
均有 `if __name__ == "__main__"` 保护；缓存、分片、快照目录由程序自动
创建，工作目录可写即可运行。
