# 房产交易消费者重平衡内核

这是一个只依赖 Python 3 标准库的本地确定性内核，用于在接入消息总线前复核消费组重平衡、位点回收、再投递、积压统计和跨 schema 归一。

## 快速运行

```bash
python3 main.py
```

默认以固定种子 `20261128` 生成三版本消费组工程、成员事件和分区键事件流，完成全部轮次的计算与自检，并在 `artifacts/` 写出报告。

```bash
python3 main.py --format json
python3 main.py --seed 20261128 --output-dir artifacts
python3 main.py --imbalance-threshold 0.25 --inflight-limit 2
python3 -m rebalance_kernel
```

运行单元测试：

```bash
python3 -m unittest discover -s tests -v
```

## 产物

| 文件 | 内容 |
| --- | --- |
| `artifacts/generated/project_v1.json` | v1：扁平分区表、0 基分区、毫秒超时 |
| `artifacts/generated/project_v2.json` | v2：按主题分组树、1 基分区、秒制超时 |
| `artifacts/generated/project_v3.json` | v3：嵌套 consumer/partition 描述、毫秒或秒单位混用 |
| `artifacts/generated/member_events.json` | 固定逻辑时刻的加入、退出、速率变更、心跳序列 |
| `artifacts/generated/keyed_events.json` | 以房源编号或办理单号稳定映射到分区的有序事件流 |
| `artifacts/generated/edge_cases.json` | 分区少于成员、同刻加入退出、超时边界、键集中度场景 |
| `artifacts/normalization_report.json` | 统一模型、逐字段来源路径、原始值差异和 canonical 摘要 |
| `artifacts/rebalance_report.json` | 主场景逐轮归属、存活依据、变化清单、再投递和积压摘要 |
| `artifacts/version_equivalence.json` | 三版本归一后逐轮运行结果摘要 |
| `artifacts/edge_cases_report.json` | 四类边界场景逐轮结果 |
| `artifacts/summary.json` | 最终 PASS/FAIL 汇总与产物索引 |

## 统一模型

装载器将三个 schema 归一为同一内容模型：

- 成员：`member_id`、`processing_rate`、`session_timeout_ms`、排序后的 `subscriptions`
- 主题：主题名与排序后的 0 基分区编号
- 消费组：`group_id`

归一过程为每个字段记录：

- 统一字段路径，例如 `members.consumer-b.session_timeout_ms`
- 原始 schema 版本
- 原始 JSON 路径，例如 `$.members[1].lease.session_timeout_sec`
- 原始值和转换说明，例如 `seconds_to_milliseconds`、`partition_base_1_to_0`

`normalization_report.json` 同时给出原始字段差异（预期存在）和 canonical 差异（必须为空）。三个版本归一后的 canonical SHA-256 相同。

## 计算规则

### 时间与会话存活

逻辑时间统一为毫秒。每轮先按当前归属推进消费账本，再在应用本轮事件前检查会话超时：

```text
存活 iff now - last_heartbeat_ms <= session_timeout_ms
```

因此心跳恰好落在 `last_heartbeat + session_timeout` 时仍存活；只有严格超过边界才失效。失效成员必须重新 `join` 才能恢复。同一时刻事件先按类型稳定排序，再统一触发一次重平衡；成员加入、主动退出、失效和速率变更均会在轮次结果中给出依据。

### 分区归属

每个主题独立计算，步骤如下：

1. 主题内每个分区用其分区键集合生成稳定 SHA-256 亲和候选成员。
2. 存活且订阅该主题的成员按处理速率进入平滑加权轮询。
3. 当前权重相同时优先亲和候选，再按成员 ID 稳定决胜。
4. 无正速率时退化为等权分配，并在报告中标记 `fallback_unweighted`。

同一分区只有一个所有者，因此落入该分区的所有房源编号或办理单号始终由同一成员顺序处理。事件流会校验 `partition = stable_hash(topic + key) % partition_count`。

### 局部重算

只有“纯处理速率变更”轮次启用局部重算：

1. 仅重算速率变更成员订阅的主题。
2. 未受影响主题保留原归属。
3. 同时刻另做一次全量重算并逐分区比较。

由于主题是独立的协调域，主场景中所有局部重算轮次均与全量重算逐分区一致，差异列表为空。

### 位点、再投递与积压

每个主题分区维护连续的 `committed_offset` 和 `next_offset`。消费推进时保留每个分区最后一组未确认事件，最多 `--inflight-limit` 条。重平衡时：

1. 找出旧所有者与新所有者不同的分区。
2. 回收这些分区 `[committed_offset, next_offset)` 内的未确认事件。
3. 按新所有者生成每条事件的唯一再投递记录。
4. 校验事件数、事件 ID 唯一性、位点连续性和分区边界。

若暂时没有存活所有者，事件仍进入再投递清单并标记 `requeued_without_owner`，不会静默丢失。报告逐轮给出重平衡前后各成员分区变化、按成员与主题的已确认位点、积压条数、未确认条数和负载偏差。

负载偏差定义为：

```text
abs(实际分区占比 - 按处理速率应得分区占比)
```

超过 `--imbalance-threshold` 的成员会出现在 `over_threshold_members`。

## 边界场景

生成器固定产出并执行以下边界输入：

- 1 个分区、3 个存活成员：只有一个所有者，其余成员为空载。
- 同一逻辑时刻 2 名成员退出、2 名成员加入：事件稳定排序后合并重平衡。
- 心跳恰好等于会话超时：判定存活；超过 1ms 判定失效。
- 60 个分区键集中到同一分区：键仍只有一个所有者，归属与统计确定。

## 代码结构

```text
main.py                         项目入口
rebalance_kernel/model.py       统一数据模型、稳定哈希、canonical JSON
rebalance_kernel/normalize.py   三版本装载、来源追踪、差异报告
rebalance_kernel/assignment.py  亲和候选与加权归属
rebalance_kernel/engine.py      时间轮、存活判定、位点账本、再投递
rebalance_kernel/generator.py   固定种子生成器与边界场景
rebalance_kernel/cli.py         本地运行入口与产物编排
tests/test_kernel.py            验收与边界测试
```

所有输出均由事件序列、配置和种子确定，不依赖系统时间、随机状态或第三方包。
