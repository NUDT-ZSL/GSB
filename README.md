# SeqLedger：两代顺序投递语义的追平与互操作工具

`seqledger` 是一个只依赖 Python 标准库的确定性数据处理与账本校验工具。
它把 v1/v2 两代顺序投递描述合并为统一视图，按注入逻辑时钟追平积压，并通过
SQLite 版本指针、文件锁和串行重算完成中断恢复与一致性验证。

本工程不访问网络。订阅声明中的回环回调端点只做地址与速率校验；投递确认由
确定性逻辑时钟和消费速率上限模拟，不会连接 `127.0.0.1` 之外的任何目标。

## 快速开始

环境要求：Linux/WSL Ubuntu，Python 3.9+，标准库自带 `sqlite3`。

```bash
# 1. 重建固定种子样本（也可直接使用仓库内 fixtures/）
python3 -m seqledger generate --out fixtures

# 2. 解析、校验并追加事件
python3 -m seqledger --db ledger.sqlite3 load \
  --topology fixtures/topology.json \
  --events fixtures/events.jsonl \
  --subscriptions fixtures/subscriptions.json

# 3. 合并两代快照，输出逐条时间换算与裁决证据
python3 -m seqledger --db ledger.sqlite3 merge \
  --v1 fixtures/semantics_v1.json \
  --v2-manifest fixtures/semantics_v2/manifest.json \
  --v2-meta fixtures/semantics_v2/meta.sqlite3 \
  --logical-clock-ms 3000 --out artifacts/merge-evidence.json

# 4. 在逻辑时钟 7000ms 追平
python3 -m seqledger --db ledger.sqlite3 replay --logical-clock-ms 7000

# 5. 稳定状态、语义补丁和串行逐位校验
python3 -m seqledger --db ledger.sqlite3 status
python3 -m seqledger --db ledger.sqlite3 patch \
  --file fixtures/patch-001.json
python3 -m seqledger --db ledger.sqlite3 verify \
  --topology fixtures/topology.json \
  --events fixtures/events.jsonl \
  --subscriptions fixtures/subscriptions.json \
  --v1 fixtures/semantics_v1.json \
  --v2-manifest fixtures/semantics_v2/manifest.json \
  --v2-meta fixtures/semantics_v2/meta.sqlite3 \
  --merge-clock-ms 3000 --logical-clock-ms 7000 \
  --patch fixtures/patch-001.json
```

全量验收：

```bash
python3 -m unittest discover -v
python3 scripts/acceptance.py --out-dir artifacts/acceptance
```

## 子命令

| 子命令 | 作用 |
| --- | --- |
| `load` | 先完整校验拓扑、事件和订阅，再逐事件事务追加偏移 |
| `merge` | 合并 v1/v2，落盘统一视图、换算台账和裁决证据 |
| `replay` | 按注入逻辑时钟推进分区内顺序投递 |
| `patch` | 应用语义代数、上游水位或墓碑修改，并只重算失效闭包 |
| `resume` | 校验当前指针后从最近已提交快照继续 |
| `status` | 稳定排序输出每分区水位、可投递偏移、积压和投递代数 |
| `verify` | 从原始输入独立串行重放，逐分区和逐事件做规范 JSON 位级比对 |
| `ack` | 幂等确认一个在途投递（补充命令） |
| `generate` | 生成固定种子样本（补充命令） |

全局参数 `--db` 默认是当前目录的 `ledger.sqlite3`。

## 输入协议

固定种子生成的样本位于：

- `fixtures/topology.json`
- `fixtures/events.jsonl`
- `fixtures/subscriptions.json`
- `fixtures/semantics_v1.json`
- `fixtures/semantics_v2/manifest.json`
- `fixtures/semantics_v2/shard-000.json`、`shard-001.json`
- `fixtures/semantics_v2/meta.sqlite3`
- `fixtures/patch-001.json`

关键约定：

- 事件 `event_seq` 全局唯一，并在输入流中严格递增；分区内偏移从 0 连续递增。
- `message_key` 使用事件的 `partition_key`，两代快照针对同一 `message_key` 仲裁。
- 分区路由为 `sha256(topic + 0x1f + partition_key)` 后对分区数取模；不依赖 Python
  `hash()`，因此跨进程、跨运行稳定。
- v1 相对秒数按
  `absolute_ms = injected_merge_clock_ms + relative_seconds * ms_per_second`
  换算，固定向下取整，并在视图记录标度、舍入方向和余量。
- 合并排序键为
  `(logical_clock_ms, semantic_generation, source_priority)`；来源优先级 v2=2、
  v1=1，仅在时钟和代数也相同时作为最终裁决。每个条目保留胜者来源、原时间、
  loser 证据和 `source_origins`。
- v2 清单校验分片 SHA-256、分片代数非降序、SQLite 元数据与分片逐行一致、
  墓碑引用和上游水位引用闭合。
- 消费速率上限以 `ceil(1000 / max_rate_per_sec)` 的毫秒间隔模拟；一个分区任一
  时刻最多一个在途投递，后偏移不能越过前偏移确认。

## 账本与恢复

SQLite 中只有一张可见指针 `ledger_pointer` 和不可变版本表
`ledger_versions(version, sha256, body)`。每次提交：

1. 获取 `<db>.lock` 文件锁并开启 `BEGIN IMMEDIATE`。
2. 从当前指针读取完整 JSON 状态。
3. 将下一版本放入 SQLite 临时 `staging` 表并校验版本号和 SHA-256。
4. 插入新不可变版本，条件更新单行指针。
5. 同一事务提交；任何异常都会回滚。

状态快照一次性包含分区水位、投递代数、在途登记、确认、死信、语义视图、
补丁历史、格式迁移决策和逻辑时钟水位。

`resume` 和 `verify` 会检查版本号连续、每个版本体哈希、指针哈希及
`max(version) == pointer_version`；指针落后于更高版本时返回
`LEDGER_VERSION_REGRESSION`。

测试专用的 `--fault-kill-after-commits` 和 `--fault-kill-before-commit` 可在
追平或合并中途发送 `SIGKILL`。恢复只从当前已提交快照继续；已提交的在途登记、
确认标记和投递声明会阻止同事件同代数重复投递。

## 补丁闭包

补丁可以同时修改：

- 消息语义条目及其投递代数；
- 消息对上游 `(topic, partition, upstream_offset)` 的水位依赖；
- 失效墓碑。

工具从直接变更键沿依赖边做传递闭包，只标记并重算受影响分区；未受影响分区的
分区状态和既有结论保持原值。输出包含：

- `affected_keys` 与 `affected_partitions`；
- `reused_partitions` 和 `reused_conclusion_count`；
- 可追溯的 `chains`，例如
  `pay-AB-002 -> pay-AB-004 -> pay-AB-006`；
- `affected_conclusions`，列出受影响的既有投递结论；
- 若补丁命中在途投递，则产生失效登记和失效来源。

## 业务错误码

所有错误输出为稳定的 JSON，包含 `code`、`message`、`diagnostics` 和
`ledger_written`。输入校验发生在账本初始化或写入之前。

典型独立错误码：

| 场景 | 错误码 |
| --- | --- |
| 主题重复 | `TOPOLOGY_DUPLICATE_TOPIC` |
| 分区数非法 | `TOPOLOGY_INVALID_PARTITION_COUNT` |
| 规则无法解析 | `TOPOLOGY_RULE_UNPARSABLE` |
| 规则未覆盖事件键 | `TOPOLOGY_RULE_UNCOVERED` |
| 事件序号重复 | `EVENT_DUPLICATE_SEQUENCE` |
| 事件乱序 | `EVENT_OUT_OF_ORDER` |
| 分区缺失 | `PARTITION_MISSING` |
| v1 相对秒数非法 | `V1_RELATIVE_SECONDS_INVALID` |
| v2 分片哈希不符 | `V2_SHARD_HASH_MISMATCH` |
| v2 代数回退 | `V2_GENERATION_REGRESSION` |
| v2 墓碑引用不闭合 | `V2_TOMBSTONE_OPEN` |
| 已合并语义代数回退 | `SEMANTIC_GENERATION_REGRESSION` |
| 补丁代数回退 | `PATCH_GENERATION_REGRESSION` |
| 账本版本回退 | `LEDGER_VERSION_REGRESSION` |
| 验证不一致 | `VERIFY_MISMATCH` |

完整列表见 `seqledger/errors.py`。

## 验收结果

实际运行 `python3 scripts/acceptance.py --out-dir artifacts/acceptance` 的结果记录在
`artifacts/acceptance/acceptance-report.json`，主要结论：

- 固定种子样本再次生成后文件 SHA-256 全部一致。
- 18 个事件、7 个分区追平后积压为 0，1 个墓碑死信，18 个唯一投递声明。
- 追平进程在第 5 个已提交版本后被 `SIGKILL`，`resume` 从指针版本 25 恢复并完成。
- 合并提交前 `SIGKILL` 后账本版本与 SHA 不变，随后合并成功。
- 补丁将 `orders/2`、`payments/0`、`payments/1` 标为失效闭包，其余 4 个分区结论复用。
- 补丁前后 `verify.bitwise_match=true`；并发追平和串行重放的状态水位一致。

逐项证据位于 `artifacts/acceptance/`：

- `merge-evidence.json`
- `status-before-patch.json`
- `verify-before-patch.json`
- `patch-invalidation.json`
- `status-after-patch.json`
- `verify-after-patch.json`
- `concurrency-status.json`
- `resume-result.json`

## 代码结构

```text
seqledger/
  cli.py          # 子命令与 JSON 输出
  validation.py   # 全部输入格式校验
  merge.py        # 两代统一视图与裁决证据
  engine.py       # 路由、追平、确认、闭包与状态
  store.py        # SQLite 临时表、不可变版本和指针
  verify.py       # 独立串行重放与逐位比对
  sample.py       # 固定种子样本/补丁生成
tests/            # 32 个标准库 unittest
scripts/
  acceptance.py   # 端到端验收与证据落盘
fixtures/         # 固定种子样本
artifacts/acceptance/ # 实际运行证据
```

实现中不读取系统墙钟；所有时间推进由 `--logical-clock-ms`、事件逻辑时间戳、
语义生效时间和补丁逻辑时钟决定。
