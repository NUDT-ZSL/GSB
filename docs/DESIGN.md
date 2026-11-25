# 多路稀疏召回融合链路技术说明

## 1. 总体结构

```
输入 JSONL/JSON
   |-- validate.py 结构化校验（业务码 + 字段路径）
   v
precision.py 算子抽象（Float64Ops / FixedOps 同语义）
   v
index.py 倒排 / 前缀桶 / 类别热度 / L2 归一化向量
   v
recall.py 四路确定性召回（同分 doc_id 升序）
   v
fusion.py min-max 归一化 -> 归并去重 -> 加权融合 -> top-k 门限
   v
diagnose.py 以 float64 为真值的四级误差账本
   v
scheduler.py 进程池并行 + 候选/内存双重预算准入
   v
cache.py 内容寻址缓存 + 增量失效 + 版本化快照（SQLite）
```

## 2. 四路召回口径

| 路名 | 稀疏特征 | 原始分口径 |
| --- | --- | --- |
| `inverted` | 词项倒排链 | sum(词项权重 / sqrt(doc_len)) |
| `prefix` | 词项前缀桶 | 前缀命中词数 / sqrt(doc_len) |
| `vector_ann` | L2 归一化向量 | 查询向量与文档向量点积（余弦） |
| `category_hot` | 文档类别 | 类别内 sqrt(doc_len) 均值的归一化热度 |

每路候选先按原始分降序、`doc_id` 升序破平，再按 `candidate_cap` 截断；
截断前全集记录在 `RouteResult.eligible_doc_ids`，供一致性自检使用。

## 3. 归一化、去重与融合

- 归一化使用 min-max：`(x - min) / (max - min)`，零跨度时整路置 0，
  避免除零并保持确定性；`min_max_epsilon` 控制零跨度判定。
- 归并按 `doc_id` 聚合四路命中，完整保留每路 raw / normalized / 路名。
- 去重口径：
  - `highest_norm`：归属路取归一化分最高者（同分按路序稳定）
  - `first_hit`：归属路取 `route_order` 中首个命中的路
- 最终分 = sum(归一化分 × 路权重)，每条加权贡献都可独立重算，
  自检要求重算值与最终分误差不超过 `1e-9`。
- top-k 截断后记录门限分数与 `threshold_guard` 保护带，
  落入门限 ± 保护带的候选进入 `near_threshold` 稳定性观察名单。

## 4. 双精度档语义

两条路径实现同一 `Ops` 协议：`add/sub/mul/div/sqrt/maximum/minimum/from_float`。

- float64：直接 IEEE-754 双精度运算，作为参考真值。
- fixed：领域整数表示 `真实值 = 整数 / scale`；
  乘除重新量化到同一缩放因子，开方使用整数牛顿取整，
  支持 `half_up / half_down / half_even / truncate` 四种显式舍入。

召回与融合代码只依赖算子协议，因此切换精度档不改变控制流；
`diagnose.py` 校验两条路径的候选顺序与归属路完全一致，
否则抛出 `E_PRECISION_DRIFT`，保证“只允许分数出现数值差异”。

## 5. 误差传播账本

对每个查询在四级对账：`raw -> normalized -> weighted -> final`。

- 绝对偏差：`|actual - reference|`
- 相对偏差：`|actual - reference| / max(|reference|, 1e-12)`
- 误差放大比：下游相对偏差 / 上游相对偏差（上游近零按地板处理）
- 主导误差来源：按各路加权级绝对偏差求和排序
- 门限稳定性：门限绝对偏差是否落在保护带内，近门限候选集合是否一致

## 6. 并行调度与预算准入

- 查询按输入顺序切成 `batch_size` 定长批次，批次下标与批内顺序固定。
- `workers > 1` 使用 spawn 进程池，worker 初始化时一次性构建索引，
  归并严格按批次下标进行，与 future 完成顺序无关。
- 候选预算：调度前按各路 cap 与 top_k 估算，超上限抛 `E_RECALL_BUDGET`；
  运行期再做一次累计防御校验。
- 内存预算：按倒排条目、前缀条目、向量分量等结构计数估算驻留字节，
  超上限抛 `E_RESOURCE_LIMIT`。估算不读 RSS，跨机器可复现。

## 7. 内容寻址缓存与失效传播

模块依赖链：

```
docs / term_weights -> index -> recall -> fusion
route_config -------^        ^
fusion_config + precision -> fusion_weights
```

每个模块键为其上游键与自身内容校验和的 SHA-256；
任意上游字节变化都会级联改变下游键。
`plan_invalidation` 对比新旧键得到变更模块、受影响查询、
重算集合与复用集合，公共模块变化影响全部查询，
查询级模块变化只影响对应查询。

## 8. 版本化快照与并发提交

SQLite 三张表：`cache_entries`、`snapshots`、`meta`。

- 每类快照维护单调递增版本号与载荷 SHA-256，读取即校验，
  损坏抛 `E_CHECKSUM`。
- 提交在 `BEGIN IMMEDIATE` 单事务内完成，整体插入新版本，
  旧快照保留可追溯；内容未变化时幂等不推进版本。
- `expected_version` 提供乐观并发控制，过期提交抛 `E_VERSION_CONFLICT`。
- 全局 `commit_seq` 跨快照类型单调递增，保证同一时刻只有一个提交生效。

## 9. 结构化业务码

| 错误码 | 触发场景 |
| --- | --- |
| `E_DUP_QUERY_ID` | 重复 query_id |
| `E_DOC_TERM_RANGE` | 文档/查询词项越界 |
| `E_WEIGHT_NON_POSITIVE` | 路权重或词项权重非正 |
| `E_BUDGET_NON_POSITIVE` | worker/批次/预算非正 |
| `E_RECALL_BUDGET` | 候选总量超预算 |
| `E_RESOURCE_LIMIT` | 驻留内存超预算 |
| `E_PRECISION_DRIFT` | 精度档切换改变顺序或去重 |
| `E_CHECKSUM` / `E_VERSION_CONFLICT` | 快照损坏或并发冲突 |
| `E_SELF_CHECK` | 跨模块一致性自检失败 |

所有异常携带 `field_path`，序列化后可直接进入结构化日志。
