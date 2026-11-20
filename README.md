# 本地事件总线流式集成中间件

这是一个完全本地、确定性、不依赖外部服务的 Python 事件总线实现。它以 SQLite
作为唯一持久化状态，使用注入的逻辑时钟推进追加、桥接、消费、保留清理与编辑版本，
并提供稳定的 JSON CLI 作为领域数据编辑和操作界面。

## 运行环境

- Python 3.11+（本机验证使用 3.11.15）
- Python 标准库 `sqlite3`（本机 SQLite 3.53.1）
- 可选 NumPy（并发验证脚本优先使用；缺失时自动回退到固定种子标准库随机数）
- 无 Docker、无网络服务、无真实账号

项目内已创建 `.venv` 并安装 NumPy 2.4.6。核心运行和端到端验证只使用标准库。

```bash
python3.11 -m eventbus --help
python3.11 -m unittest discover -s tests -v
```

也可以安装为本地命令：

```bash
.venv/bin/python -m pip install -e .
.venv/bin/eventbus --help
```

## 目录

```text
eventbus/              核心实现
  models.py            样本解析、业务校验、桥接环检测
  expr.py              受限表达式编译器与求值器
  store.py             SQLite schema、事务、状态摘要
  service.py           追加/桥接/消费/清理/编辑/恢复/验证
  cli.py               稳定 JSON CLI
samples/               固定 JSON 样本与固定种子场景
scripts/               双运行对比、崩溃恢复、并发验证
tests/                 11 项标准库 unittest
```

## 固定样本

- `samples/catalog.json`：3 个主题/通道及分区数、保留字节上限、单批上限
- `samples/bridges.json`：3 条无环桥接规则，包含过滤、变换和上游水位依赖
- `samples/events.json`：8 条严格递增序号的生产者事件
- `samples/subscriptions.json`：3 个消费者订阅及处理预算
- `samples/scenario.json`：固定种子 `149`、固定逻辑时钟和消费轮次

样本桥接图：

```text
orders-main   -> payments-main
orders-main   -> audit-main
payments-main -> audit-main
```

`orders-to-payments` 过滤大额订单，`payments-to-audit` 在支付水位就绪后继续级联。
`depends_upstream_offset` 表示规则可运行的最低上游末尾 offset：
`upstream.end_offset >= depends_upstream_offset` 后才允许转发。

桥接表达式使用受限 Python 表达式语法，不执行属性访问、导入或任意函数调用。
可用名称为 `payload`、`event`、`ops`，可用函数仅限
`abs/int/len/lower/max/min/round/str/upper`。过滤表达式必须返回布尔值，
变换表达式必须返回 JSON 对象；变换结果会被求值两次并比较规范摘要。

## 快速开始

```bash
DB=/tmp/eventbus-demo.db

python3.11 -m eventbus load \
  --db "$DB" --samples samples --clock 11000

python3.11 -m eventbus bridge \
  --db "$DB" --clock 12000

python3.11 -m eventbus consume \
  --db "$DB" --consumer ledger --clock 14000

python3.11 -m eventbus cleanup \
  --db "$DB" --clock 21000

python3.11 -m eventbus status --db "$DB"

python3.11 -m eventbus verify \
  --db "$DB" --samples samples
```

`load` 在单个事务中完成配置快照、源事件、订阅和各通道首批事件追加。后续
`publish` 只追加新增且尚未发布的源事件；完全相同的事件不会重复追加。

## CLI

所有成功和错误输出都是单行、键排序、无空白的 JSON。退出码 `0` 表示成功，
`2` 表示业务错误，`90` 表示测试注入的强制终止，`130` 表示中断。

| 命令 | 作用 |
| --- | --- |
| `load` | 校验固定样本并原子装载、追加首批事件 |
| `publish` | 追加新增/编辑后尚未发布的事件 |
| `bridge` | 按绑定配置版本执行过滤、变换和级联转发 |
| `consume` | 按预算拉取事件并自动或显式回报位点 |
| `cleanup` | 执行保留字节压力下的物理清理/墓碑标记 |
| `edit` | 事件或桥接规则的 add/update/delete，输出影响闭包 |
| `resume` | 重放崩溃后保持 `pending` 的操作意图 |
| `status` | 输出稳定排序的通道、桥接、订阅和清理状态 |
| `verify` | 校验配置、日志、桥接、订阅、清理及输出摘要 |
| `digest` | 输出样本规范摘要 |

常用错误码包括：

```text
E_BAD_CHANNEL E_DUPLICATE_TOPIC E_DUPLICATE_CHANNEL
E_BAD_PARTITIONS E_BAD_RETENTION E_BAD_BATCH_SIZE
E_BAD_RULE E_BRIDGE_CYCLE E_EVENT_SEQ E_BAD_CLOCK
E_OFFSET_AHEAD E_UPSTREAM_WATERMARK_NOT_READY
E_UPSTREAM_WATERMARK_REGRESSION E_CHANNEL_VERSION_MISMATCH
E_RULE_VERSION_CHANGED E_CLOCK_REGRESSION E_SAMPLE_DIGEST
E_TRANSFORM_DIGEST E_REPORT_AHEAD E_SUBSCRIPTION E_VERIFY
```

## 确定性规则

- 分区键规范化为 `str:...` 或 `int:...`，使用固定 person 的 BLAKE2b-64
  对分区数取模；同一键在任何运行中落到同一分区。
- JSON 载荷、事件摘要、批次摘要、配置摘要和状态摘要均使用
  `sort_keys=true`、固定分隔符和 UTF-8 的规范 JSON。
- 通道内按全局事件序号排序分配连续 offset；每通道按
  `max_batch_records` 切批。
- `status.channels[].log_digest` 按 offset 稳定汇总该通道的 offset、分区、
  载荷/源摘要、时钟、墓碑状态与来源，可用于逐字节比对。
- 配置、桥接规则、订阅以及所有输出均按业务键排序。
- 不读取系统墙钟。命令传入的逻辑时钟必须单调不减。
- offset 的含义是“下一条待处理事件位点”；`tail_offset = end_offset - 1`。

## 事务、恢复与并发

每次变更遵循同一路径：

1. 写入带完整输入的 `operations` 意图行。
2. 获取跨进程文件锁。
3. 用 SQLite `BEGIN IMMEDIATE` 单事务写入业务状态、逻辑时钟、提交序号和
   `committed` 操作状态。
4. 进程若在意图写入后、事务提交前被强制终止，SQLite 回滚业务写入，只保留
   `pending` 意图；`resume` 用原输入重放。

桥接动作以 `(rule_id, source_offset)` 唯一，生产事件以 `(topic, event_seq)`
唯一，通道日志以 `(channel, offset)` 唯一。重复批次不会再次分配 offset；
重复桥接不会再次推进桥接位点；订阅位点只能单调前进。

配置快照通过 `config_pointer` 整体切换。桥接操作意图保存开始时的配置版本、
规则内容摘要和完整规则表达式；恢复时版本不一致返回 `E_RULE_VERSION_CHANGED`，
不会混用新旧规则。读事务通过 SQLite 快照读取单一配置版本。

强制终止注入：

```bash
EVENTBUS_CRASH_AT=after_intent          # 意图已提交、业务事务未开始
EVENTBUS_CRASH_AT=after_load_state      # load 事务中间
EVENTBUS_CRASH_AT=after_append_batch    # 追加事务中间
EVENTBUS_CRASH_AT=after_bridge_stage    # 桥接事务中间
EVENTBUS_CRASH_AT=after_consume_stage   # 位点回报事务中间
EVENTBUS_CRASH_AT=after_cleanup_stage   # 清理事务中间
EVENTBUS_CRASH_AT=before_commit         # 提交前
```

## 清理语义

每个通道的清理安全水位为以下最小值的组合：

```text
通道末尾
全部订阅的 current_offset
全部桥接规则的 next_offset
```

仅当 live payload 字节超过 `retention_bytes` 时回收。被桥接动作引用的上游或
下游记录保留为墓碑（清空 payload、保留 offset/摘要），可直接删除且不存在桥接
血缘的记录执行物理删除。清理汇总包含安全水位、清前/清后字节、物理删除数、
墓碑数、清理区间与保留原因，并可在 `status.cleanup.last_run` 和
`cleanup_runs` 表查询。

`load`、`publish`、`bridge`、`consume` 在同一事务提交前自动执行清理裁决，保证
保留上限在状态推进后立即生效；显式 `cleanup` 命令用于独立审计并记录
`within_limit` 结果。

## 编辑语义

- 规则编辑立即生成新的完整配置版本和内容摘要，并原子切换指针。
- 未发布事件可修改后由 `publish` 按新内容追加；发布后的事件日志不可改写。
- 已发布事件的 update/delete 会生成可追溯编辑版本，并返回
  `published_log_immutable=true`；原日志保留，避免破坏 offset、消费者位点与桥接
  位点的一致性。
- 所有编辑写入 `edit_versions`，包括操作类型、前后内容、影响闭包、配置版本、
  逻辑时钟和操作幂等键。

## 实际验证

最终验证结果记录在 `VALIDATION.md`，复现命令：

```bash
python3.11 -m unittest discover -s tests -v
PYTHON=python3.11 ./scripts/run_twice.sh
python3.11 scripts/crash_recovery.py
.venv/bin/python scripts/concurrency_check.py
```
