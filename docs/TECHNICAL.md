# 技术设计

## 模块边界

| 模块 | 职责 |
| --- | --- |
| `qxworkbench.rules` | v1/v2/v3 探测、解析、稳定写出、语义校验和 |
| `qxworkbench.migration` | 升级默认补全、降级展平/丢弃报告、回读 |
| `qxworkbench.bridge` | 桥接资源校验、内容校验和、受影响目标语言差异 |
| `qxworkbench.expand` | 规则触发、嵌套条件、有限深度桥接 BFS、候选排序 |
| `qxworkbench.indexer` | 文档校验、分区构建、posting 来源、复用 retag |
| `qxworkbench.scoring` | BM25、置信度加权、贡献合并、基线差谱、流式路径 |
| `qxworkbench.snapshots` | 不可变快照、提交链、交叉校验、原子 `CURRENT` |
| `qxworkbench.workbench` | 构建会话、进程池、崩溃恢复、查询编排 |
| `qxworkbench.selfcheck` | 版本、来源、顺序、贡献和恢复集合一致性 |
| `qxworkbench.samplegen` | 固定种子样本、规则和桥接 |

## 稳定字节与校验和

所有 JSON 使用：

```text
ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
```

对象末尾补一个换行。数组和对象先按业务键排序，例如规则按 `rule_id`、分区按语言、posting 按 `(doc_id, term)`。校验和是稳定字节的 SHA-256。

规则语义校验和排除：

- 源文件路径和行号；
- 外层格式版本（v1/v2/v3 的同一可表达语义可共享 checksum）；
- 未提供的可选字段（保留“缺失”与“显式默认值”的区别）。

桥接内容校验和排除：

- 发布版本号；
- 存储的 `checksum` 字段本身。

因此“版本号变化但内容不变”和“内容变化”是两个可独立检测的维度。

## 规则迁移不变量

### 升级

`v1 -> v2 -> v3`：

- 核心字段不改变；
- 可选字段缺失时补 schema 默认值，并记录来源；
- `condition_terms` 在 v3 中物化为 `all(term_present...)`；
- 已存在字段不回填、不覆盖。

### 降级

`v3 -> v2 -> v1`：

- 多目标必须显式选择 `flatten` 或 `drop`；
- 条件只有“纯 `term_present` 合取”可由 v2 表达；
- 其余条件必须丢弃并报告，不能悄悄把 `any` 改写成 `all`；
- v1 无条件表达能力，因此全部条件记录为丢弃；
- v1 不表达 v2 的可选字段，因此这些字段也记录为丢弃。

测试的往返断言只比较目标格式可表达字段，不要求 v1 保留 v3 条件。

## 桥接路径算法

状态为 `(language, term)`。从 `(source_lang, source_term, confidence=1)` 出发，按最大置信度优先队列遍历桥接边：

- 每条边将置信度相乘；
- 路径长度不超过规则深度；
- 同一 `(language, term)` 只保留更高置信度；置信度相同时保留更短、字典序更小的路径；
- 到达目标语言时记录候选；
- 用路径中已访问的目标边阻止简单环。

对相同目标词项去重时选择最高有效置信度。输出 `alternate_paths` 记录同分路径，而不是把它们重复计分。

## 打分可重算性

每个贡献在写入结果前分别舍入到 12 位小数，最终分也只使用这些已输出的贡献：

```text
sum: score = round(sum(detail.contribution), 12)
max: score = round(max(detail.contribution), 12)
```

这可避免“明细是 12 位、总分由未舍入值求和”造成的不可复算差异。基线贡献与改写贡献都带 `source`，可反查源 JSONL。

## 版本协同

提交链包含：

```text
commit_id = sha256(core)[:32]
core = {
  rule_version, rule_checksum,
  bridge_version, bridge_checksum,
  index_version, index_checksum,
  affected_partitions, reused_partitions, recomputed_partitions,
  degraded, snapshot_mode
}
```

三层校验：

1. 文件层：manifest 校验和、分区 SHA-256、规则/桥接语义校验和；
2. 引用层：commit、index manifest 与规则/桥接快照的版本/校验和交叉相等；
3. 提交层：commit id 必须等于核心内容散列。

`CURRENT` 是单文件指针。快照目录先完整落盘，commit 文件再落盘，最后原子替换 `CURRENT`。查询只从一次读取到的 commit 定位三层快照，因此不存在查询过程中切换源。

## 恢复状态机

构建状态：

```text
building -> committed
```

每次完成一个分区：

1. 原子写分区文件；
2. 原子更新 `state.json` 中该分区校验和；
3. 若达到故障注入阈值，直接终止。

未写入 state 的分区文件不视为已提交。重启校验 state 中每个分区文件：

- 缺失或校验失败：移除完成记录；
- 完成且桥接差异未涉及：复用；
- 完成但桥接差异涉及：移除完成记录并重算。

桥接差异只取变更词项的目标语言。恢复时先比较“中断会话桥接”与“当前桥接”，而不是与更早的已提交桥接比较，避免重复重算。

## 并发和资源

分区构建使用 `ProcessPoolExecutor.map`，提交顺序仍按输入语言排序。最大 worker 数为：

```text
min(requested_workers, max_parallel_partitions, pending_partitions)
```

并行预算的判定使用本次提交的受影响分区数，而不是恢复后剩余分区数，因此同一目标配置在不间断运行和恢复运行中得到相同降级标记。

查询对内存预算的判断使用索引 manifest 中所有分区文件大小之和。超限时按语言逐个加载、打分、释放；不改变候选遍历和文档遍历顺序，因此分数和明细与驻留路径一致。

## 自检失败策略

`run_selfcheck` 先聚合所有类别，再在 `strict=True` 时抛 `E_INCONSISTENT`。无法打开活动快照或分区校验失败也会转换成 `E_INCONSISTENT`，同时保留底层错误码：

```json
{
  "code": "E_INCONSISTENT",
  "details": {
    "categories": ["INDEX_SNAPSHOT"],
    "underlying_code": "E_VERSION_SKEW"
  }
}
```

普通查询/加载 API 仍直接返回 `E_VERSION_SKEW`，两类入口的语义边界明确。
