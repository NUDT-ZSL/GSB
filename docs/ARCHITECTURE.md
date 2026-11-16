# 多路召回与融合排序实验工作台 — 技术说明

## 1. 模块结构

| 模块 | 职责 |
| --- | --- |
| `recall_bench/util.py` | 规范化 JSON、稳定哈希、文件指纹、临时文件+原子替换 |
| `recall_bench/config.py` | 配置 schema、默认值、校验、按子域派生参数指纹 |
| `recall_bench/samplegen.py` | 固定种子样本生成（冷启动用户、长尾物品、周期热度、故障行注入）与规模切片 |
| `recall_bench/parser.py` | 交互日志/词典解析、四类业务码、确定性索引与统计 |
| `recall_bench/channels.py` | 词面、协同余弦、时间衰减热度三路召回，同分按物品 ID 字典序 |
| `recall_bench/fusion.py` | 归一化、加权融合与 RRF、权重重分配、名次向量决胜 |
| `recall_bench/selector.py` | 基于规模指标的预置策略表、评分与可审计决策 |
| `recall_bench/cache.py` | 内容寻址缓存键派生与复用/重算台账 |
| `recall_bench/snapshot.py` | 阶段快照、配置哈希校验、原子持久化、退出信号控制 |
| `recall_bench/evaluation.py` | 留一法 holdout、确定性抽样、离线指标 |
| `recall_bench/concurrency.py` | 带稳定键的线程池，结果按键排序回收 |
| `recall_bench/engine.py` | 单规模档流水线编排、缓存失效传播、用户级异常隔离 |
| `recall_bench/runner.py` | 多规模并发编排、报告组装 |
| `recall_bench/cli.py` | `generate`/`run`/`verify` 子命令与整数退出码 |

## 2. 解析与业务码

- `E_LOG_INVALID`：JSON 损坏、用户/物品 ID 为空、交互类型不在枚举、时间戳不可解析、词典词项为空。
- `E_LOG_DUPLICATE`：`(user_id, item_id, timestamp, interaction_type)` 完全重复。
- `E_LOG_ORDER`：同一用户时间戳相对前一条回退。
- `E_ITEM_MISSING`：交互引用了词典中不存在的物品。

失败行进入失败清单且不进入索引；其余行构建用户→物品、物品→用户、物品→词项索引，
统计每用户/每物品交互数与全局时间跨度。

## 3. 召回通道

- 词面：用户历史物品词项向量与物品词项向量的余弦相似度。
- 协同：用户-物品二分图（二值）上的物品共现余弦相似度矩阵。
- 热度：以全局最大时间为参考点的指数时间衰减交互热度（半衰期可配置）。

每通道受 `channel_limits` 约束，过滤用户已交互物品，同分按物品 ID 字典序；
通道关闭或数据不足返回空候选并记录 `skip_reason`；全程无随机打散。

## 4. 融合与决胜

- 归一化采用跨该规模全体用户的通道级 min-max（退化区间映射为 0/1）。
- `weighted`：通道归一化分按（重分配后的）权重求通道分，再与排序特征分按
  `fusion.weighted_mix` 混合；排序特征含三路归一化分与新鲜度。
- `rrf`：`sum_k weight_k / (rrf_k + rank_k)`，同样与特征分按 `1 - weighted_mix` 混合。
- 某通道候选为空时，其权重按 `proportional`（按其余通道原权重占比）或 `equal`
  重分配，明细写入 `redistribution`。
- 最终同分依次按最终分降序、融合前各通道名次向量字典序、物品 ID 字典序决断。

## 5. 规模自适应选择

指标含用户数、物品数、交互数、交互密度、冷启动用户比例、长尾比例。
预置四策略 `cold_bootstrap`/`dense_balanced`/`large_sparse`/`small_default`，
按阈值条件加分，分数相同按策略 ID 字典序，保证同输入同决策，并输出全部候选评分与理由。

## 6. 增量复用与失效传播

缓存键由上游内容指纹与参数指纹逐级派生：

```
parsed -> recall(每通道) -> normalization -> fusion -> evaluation
                 \__________________/  \________/
```

- 切换规模档：输入指纹变化，仅该档全链路重算，其它档复用。
- 通道开关：仅该通道召回键与其下游归一化/融合/评估失效。
- 融合权重：召回键不含融合权重，召回产物复用，仅融合与评估重算。
- 融合策略/排序权重：仅融合与评估失效。

复用与重算清单写入报告 `incremental` 字段。

## 7. 并发一致性

所有任务携带 `scale::id` 或通道级稳定键；线程池结果在回收时按键排序，
`max_workers=1` 与多线程产出的指标表、候选列表、失败清单逐字段一致（测试覆盖）。

## 8. 快照、恢复与异常

- 快照以临时文件 + `os.replace` 原子替换，含阶段名、配置哈希、规模档、用户游标、产物摘要、版本号。
- 恢复时版本不符抛 `E_SNAPSHOT_VERSION`，配置哈希或规模档不符抛 `E_CONFIG_MISMATCH` 并拒绝恢复、删除陈旧快照后重建。
- 单用户异常被捕获为 `E_STAGE_USER`（含用户 ID、阶段、异常类型、消息），其余用户继续。
- `SIGTERM`/`SIGINT` 置位关停标志，引擎在快照间隔点完成当前快照后以退出码 0 结束；
  强制终止后只有完整替换的快照可被读取（临时文件不会被加载）。

## 9. 评估方法

对确定性抽样用户，剔除每用户最后 `holdout_last_n` 条交互构建训练视图，
在训练视图上重跑召回融合，计算 Recall@K、Precision@K、HitRate@K、
目录覆盖率、长尾覆盖率与冷启动空推荐率。

## 10. 确定性保证

随机源仅用于样本生成（固定种子的 `random.Random` 与 `numpy.default_rng`），
召回/融合/评估路径无随机调用；所有集合在排序前转为有序序列，
浮点统一在序列化边界保留 10 位小数，规范化 JSON 保证跨运行字节级稳定。
