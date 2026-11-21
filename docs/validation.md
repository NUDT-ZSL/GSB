# 验证报告

环境：Python 3.10.12、NumPy 1.26.4、Linux（32 核），全程本地执行、无网络访问。
复现入口：`bash run_demo.sh`（或分步执行 `python -m recall_fusion.cli ...`）。

## 1. 自动化测试

`python -m unittest tests.test_pipeline`：20 个用例全部通过，
`python -m pyflakes recall_fusion tests` 无告警。覆盖：

* 数据集与向量的确定性生成、四路召回可复现与截断标记；
* 两种归一化、两种去重口径、融合分逐明细独立重算、显式空路；
* 非法归一化口径与负权重返回 `E_CONFIG_INVALID` 并定位字段；
* 预算降级路径可复现、`E_RECALL_BUDGET` 与 `E_RESOURCE_LIMIT` 拒绝；
* 快照整体替换、回退 `E_SNAPSHOT_ROLLBACK`、损坏 `E_SNAPSHOT_CORRUPT`、
  同目录重跑版本号单调递增；
* 端到端重跑逐字节一致、内存淘汰留痕、worker 异常隔离、环境变量覆盖。

## 2. 默认配置实跑产物（artifacts/demo/run_w1）

固定种子 20261122，400 篇文档、48 条查询、32 维向量：

| 指标 | 实测值 |
| --- | --- |
| 查询总数 / 准入 / 拒绝 / 失败 | 48 / 29 / 19 / 0 |
| 计划状态 | ok=25，degraded=4，rejected=19 |
| 融合阶段 | two_stage=25，single_stage=4 |
| 实际候选总数 / 比较总数 | 3000 / 3048 |
| 内存上限 / 峰值驻留 | 184320 B / 205504 B |
| FIFO 分片淘汰次数 | 29（分片 30 个，含最终 drain） |
| 拒绝错误码 | 全部 `E_RECALL_BUDGET`，原因写入 rejected.jsonl |

降级按 `degrade_priority`（vector→prefix→category→inverted）逐层对半
缩减，四个路均出现降级；每条降级步骤含 `from_limit/to_limit/reason`，
被拒查询在 `results.jsonl` 中保留显式拒绝记录（四路
`empty_reason=query_rejected`），无静默丢失。

## 3. 并行重跑逐字节一致

`run_w1`（1 worker）与 `run_w3`（`RF_WORKERS=3 RF_BATCH_SIZE=6`）：

```
results.canonical.jsonl sha256 =
97ff7d99cad80d8944978699f212b8acf1e7cc14a601974b53f15443ff3d1021  （两侧相同）
byte_identical = true
```

另有 fp32 精度标记实跑（`RF_PRECISION=fp32`）通过，精度写入配置与
`norm_params` 快照。

## 4. 快照版本化验证（snapshot_demo_report.json）

* v1→v2 提交后当前指针整体替换为 v2，v1 文件保留且不可覆盖；
* 以 v1 再次提交触发 `E_SNAPSHOT_ROLLBACK`；
* 篡改 v2 载荷后加载触发 `E_SNAPSHOT_CORRUPT`，字段定位
  `fusion_weights@v2` 并给出存储/实际两个校验和；
* 同目录重复执行引擎时版本自动 1→2 递增（回归测试覆盖）。

## 5. 产物清单

每次 `run` 输出：

* `sample/`：确定性 documents.jsonl、queries.jsonl、config.json；
* `results.jsonl` / `results.canonical.jsonl`：逐查询四路明细与融合结果；
* `cache/plan_ledger.jsonl`：每查询准入/降级/策略路径；
* `cache/rejected.jsonl`、`cache/failures.jsonl`：拒绝与 worker 异常；
* `cache/dispatch.jsonl`：确定性分块派发记录；
* `cache/memory_ledger.jsonl`：retain/evict/drain 资源账本；
* `cache/parallelism.json`：并行度冲突裁决；
* `shards/`：FIFO 淘汰分片（保留完整查询明细）；
* `snapshots/`：三类版本化快照与 manifest；
* `run_summary.json`：预算/内存/统计汇总与产物索引。
