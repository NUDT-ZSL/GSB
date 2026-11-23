# 本地事件总线流式集成中间件

这是一个不依赖在线服务或容器的事件总线桥接中间件，用于在确定性样本上验证：

- v1 单文件 JSON 与 v2 分片 JSON + SQLite 元数据的两代事件互操作。
- v1 相对秒数到绝对毫秒时刻的无损换算及换算痕迹。
- 同一事件跨两代出现时的逻辑时钟/通道代数裁决。
- 确定性分区、连续偏移、批量追加和摘要持久化。
- 过滤/变换桥接、上游偏移水位、代数与配置版本提交门禁。
- 强制终止后的操作意图恢复、桥接位点幂等和重放校验。
- 多进程生产/消费/编辑并发，以及基于 SQLite 事务和文件锁的互斥。
- 注入逻辑时钟下的保留清理，区分物理清除和仅墓碑标记。

## 快速开始

目标环境为 Windows 10/11 + Python 3.11+；当前实现也兼容 Python 3.10。

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python scripts\generate_samples.py
python scripts\run_demo.py
pytest -q
```

Linux/macOS：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/generate_samples.py
python scripts/run_demo.py
pytest -q
```

`scripts/generate_samples.py` 只使用本地确定性数据，重建：

- `samples/v1/events.json`
- `samples/v2/shards/*.json`
- `samples/v2/metadata.sqlite`
- `samples/patches/*.json`
- `samples/append-events.json`
- `samples/invalid/*.json`

## CLI

所有命令输出 UTF-8、键排序稳定的 JSON。业务错误退出码为 `2`，`verify`/`resume` 检测到未决状态时退出码为 `3`，未处理异常为 `1`。

```bash
python -m eventbus --db data/demo.sqlite load \
  --v1 samples/v1/events.json \
  --v2-shards samples/v2/shards \
  --v2-metadata samples/v2/metadata.sqlite \
  --clock-ms 1000

python -m eventbus --db data/demo.sqlite status
python -m eventbus --db data/demo.sqlite bridge --all --clock-ms 2000
python -m eventbus --db data/demo.sqlite consume \
  --subscription orders-reader --count 7 --request-id demo-7 --clock-ms 3000
python -m eventbus --db data/demo.sqlite cleanup \
  --clock-ms 4000 --retention-bytes 1000
python -m eventbus --db data/demo.sqlite verify
```

配置编辑以补丁方式产生新版本并整体切换指针：

```bash
python -m eventbus --db data/demo.sqlite edit \
  --patch samples/patches/toggle-audit-tag.json --clock-ms 5000
python -m eventbus --db data/demo.sqlite publish --version 1
```

运行时追加事件：

```bash
python -m eventbus --db data/demo.sqlite publish \
  --channel orders --events samples/append-events.json \
  --max-batch 1 --clock-ms 6000
```

桥接位点回报用于检测明确回退；正常推进仍由 `bridge` 在摘要校验后完成：

```bash
python -m eventbus --db data/demo.sqlite bridge orders-to-audit \
  --report-offset 3
```

恢复与本地 HTTP 接口：

```bash
python -m eventbus --db data/demo.sqlite resume
python -m eventbus --db data/demo.sqlite serve --host 127.0.0.1 --port 5000
```

`--db` 可放在子命令前后。默认数据库为 `data/eventbus.sqlite`。

## 输入格式

### v1

顶层字段：`format=eventbus-v1`、`epoch_ms`、`channels`、`bridges`，可选 `retention_bytes`、`clock_tolerance_ms`。

```json
{
  "format": "eventbus-v1",
  "epoch_ms": 1700000000000,
  "channels": [{
    "name": "orders-legacy",
    "canonical_name": "orders",
    "generation": 1,
    "partitions": 3,
    "event_fields": ["order_id", "region", "amount", "note"],
    "events": [{
      "sequence": 1,
      "relative_seconds": 0,
      "partition_key": "order-1001",
      "logical_clock": 100,
      "fields": ["O-1001", "east", 10.5, "seed-v1"]
    }],
    "subscriptions": [{"name": "orders-reader", "start_offset": 0}]
  }],
  "bridges": []
}
```

`relative_seconds` 必须为非负整数，换算公式为：

```text
absolute_ms = epoch_ms + relative_seconds * 1000
```

事件行持久化 `time_conversion_json`，包含 `scale`、`rounding_direction`、`rounding_remainder_ms`、`epoch_ms`、`absolute_ms` 和原相对秒数。

### v2 分片

```json
{
  "format": "eventbus-v2-shard",
  "shard": "orders-000",
  "channel": "orders",
  "generation": 2,
  "events": [{
    "sequence": 1,
    "partition_key": "order-1001",
    "logical_clock": 100,
    "produced_at_ms": 1700000000000,
    "fields": {"order_id": "O-1001", "amount": 10.5},
    "tombstone": false,
    "field_checksum": "sha256:..."
  }],
  "checksum": "sha256:..."
}
```

- `field_checksum` 是 `SHA256(canonical_json(fields))`。
- 分片 `checksum` 是除自身 `checksum` 字段外整个分片对象的规范化 JSON 摘要。
- 分片、事件索引与 SQLite 元数据必须三方一致。

### v2 SQLite 元数据

`samples/v2/metadata.sqlite` 包含：

- `meta(key,value)`：格式、保留上限、逻辑时钟容差。
- `channels`：规范名、通道代数、分区数、字段信封 schema、v1 别名。
- `event_index`：规范名、序号、代数、分片名/摘要、字段信封 JSON、字段校验和、绝对毫秒戳、逻辑时钟、墓碑标记。
- `bridges`：上下游、代数、最小上游偏移水位、过滤/变换规则、批次上限。
- `subscriptions`：订阅名、通道、起始偏移。

## 核心语义

### 跨代合并

同一 `(canonical_channel, sequence)` 的比较键为：

```text
(logical_clock, generation, source_priority)
source_priority: v2 > v1
```

即先比逻辑时钟；相同再比通道代数；仍相同则 v2 优先。每个事件保留：

- 来源版本与来源代数；
- 选中版本的原始表示；
- 候选排名、裁决依据；
- v1 路径读取时缺失的 v2 字段默认填充明细。

v1 扁平数组按声明的 `event_fields` 无损映射为 v2 具名字段信封；v2 新增字段在 v1 事件选中时按字段 schema 默认值补齐，并写入兼容降级清单。

### 分区与追加

分区路由：

```text
partition = int.from_bytes(SHA256(channel + "\0" + partition_key)[:8], "big") % partitions
```

事件偏移为通道内全局、连续、单调递增值。每次追加在一个 `BEGIN IMMEDIATE` 事务中写 offset、载荷摘要、逻辑时钟、代数与批次记录。相同序号、相同载荷重复提交是幂等；不同载荷返回 `DATA_CONFLICT`。

### 桥接与水位

单次桥接在一个事务内：

1. 复核开始绑定的配置版本。
2. 校验上下游代数与最小上游水位。
3. 逐条校验源事件已提交且载荷摘要一致。
4. 按固定顺序执行过滤与变换，数值统一用 `Decimal` 和固定舍入方向，避免平台浮点差异。
5. 写下游事件、投递摘要、扫描检查点和桥接位点。
6. 任一步失败整体回滚，不部分推进。

不同拒绝原因使用不同错误码：

- `BRIDGE_POSITION_REWIND`
- `BRIDGE_UPSTREAM_WATERMARK`
- `GENERATION_MISMATCH`
- `CONFIG_CHANGED`
- `BRIDGE_DIGEST_MISMATCH`

`bridge_deliveries` 的唯一键 `(bridge_id, source_offset)` 确保重复恢复不会重复转发。

### 保留清理

某通道的安全清理上界：

```text
safe = min(该通道所有 active 订阅偏移, 所有以该通道为上游的 active 桥接位点)
```

无读者时 `safe = high_watermark`。超过保留字节上限时：

- `offset < safe` 的普通事件：物理删除，记录 `physically_removed`。
- `offset < safe` 的墓碑事件：清空载荷并保留墓碑行与偏移骨架，记录 `tombstone_marked`。
- 无法越过的读者：记录 `blocked`、`blocked_slowest` 和保留原因。

逻辑时钟回拨超过 `clock_tolerance_ms` 返回 `CLOCK_REWIND`，整个清理事务回滚。

### 恢复与并发

- SQLite 使用 WAL、`synchronous=FULL`、30 秒 busy timeout。
- 所有写路径叠加跨进程文件锁；SQLite 事务提供数据库级互斥。
- 追加、消费、清理、整轮桥接先提交 `operation_intents` 意图。
- 桥接另写 `bridge_runs` 意图，效果与完成标记在同一事务提交。
- 崩溃在提交前：意图为 `pending`，没有业务效果，`resume` 只重做该操作。
- 崩溃在提交后：意图已完成，`resume` 不重复偏移或转发。
- 配置版本通过临时表写入 `config_versions`，最后只更新 `config_pointer` 单行指针。
- 转发在开始时绑定版本；提交前再次检查指针，变化则返回 `CONFIG_CHANGED`。

## 错误码

| 错误码 | 含义 |
|---|---|
| `INVALID_JSON` | JSON 无法解析 |
| `INVALID_FORMAT` | 文档格式/版本/字段结构非法 |
| `CHANNEL_DUPLICATE` | 通道名或别名冲突 |
| `SEQUENCE_DUPLICATE` | 通道内事件序号重复 |
| `PARTITIONS_INVALID` | 分区数不是正整数 |
| `BRIDGE_CHANNEL_MISSING` | 桥接引用了不存在通道 |
| `BRIDGE_CYCLE` | 桥接图有环 |
| `FIELD_COUNT_MISMATCH` | v1 扁平字段个数与声明不符 |
| `V1_TIME_INVALID` | v1 相对秒数非法 |
| `V2_CHECKSUM_MISMATCH` | v2 字段信封校验和不匹配 |
| `SHARD_HASH_MISMATCH` | v2 分片哈希不匹配 |
| `V2_CLOCK_INVALID` | 逻辑时钟类型/值非法 |
| `SUBSCRIPTION_RANGE` | 订阅声明起始偏移越界 |
| `MALFORMED_EVENT` | 事件结构畸形 |
| `METADATA_INVALID` | v2 SQLite 元数据不一致 |
| `DATA_CONFLICT` | 同一序号对应不同载荷 |
| `OFFSET_CONFLICT` | 持久偏移冲突 |
| `BRIDGE_POSITION_REWIND` | 桥接位点回报回退 |
| `BRIDGE_UPSTREAM_WATERMARK` | 上游水位不足、回退或事件已缺失 |
| `BRIDGE_DIGEST_MISMATCH` | 源/变换/投递摘要校验失败 |
| `GENERATION_MISMATCH` | 桥接声明代数与通道代数不一致 |
| `CONFIG_CHANGED` | 转发期间配置版本变化 |
| `CONFIG_VERSION_INVALID` | 配置版本不存在或不可切换 |
| `CLOCK_REWIND` | 逻辑时钟回拨超过容差 |
| `SUBSCRIPTION_OFFSET_OUT_OF_RANGE` | 订阅偏移越界 |
| `SUBSCRIPTION_REWIND` | 订阅偏移回退 |
| `SUBSCRIPTION_CONFLICT` | 同一请求 ID 对应不同偏移 |
| `EVENT_LOCKED` | 已消费/已桥接事件不允许编辑 |
| `PATCH_INVALID` | 编辑补丁非法 |
| `DATASET_CONFLICT` | 现有数据库来自不同样本 |
| `VERIFY_FAILED` | 持久水位与重放结果不一致 |

## 代码结构

| 路径 | 职责 |
|---|---|
| `eventbus/validation.py` | v1/v2 解析、校验、跨代合并 |
| `eventbus/expressions.py` | 确定性过滤/变换 DSL |
| `eventbus/store.py` | SQLite schema、事务、配置版本临时表 |
| `eventbus/engine.py` | 追加、桥接、消费、清理、恢复、状态、verify |
| `eventbus/cli.py` | 命令行 |
| `eventbus/api.py` | 本地 Flask JSON API |
| `scripts/generate_samples.py` | 确定性样本生成 |
| `scripts/run_demo.py` | 端到端可重复演示 |
| `tests/` | 单元、并发、强杀恢复测试 |

## 实际验证

最近一次实际执行结果见 [VERIFICATION.md](VERIFICATION.md)：

- `pytest -q`：`29 passed`
- 样本加载：3 通道、8 事件、1 桥接、3 订阅、6 条兼容降级
- 桥接：4 条投递、3 条过滤，位点 `0 -> 7`
- 清理：6 条物理清除、1 条墓碑标记，最慢阻塞位点 `audit-reader@0`
- 清理和桥接后 `verify`：`ok=true`，无 errors
- `bridge_before_commit` 强杀后 `resume`：`ok=true`，桥接位点恢复至 `7`
- 4 个并发生产者：退出码全 `0`，偏移连续，`verify=true`

## 测试专用故障注入

`EVENTBUS_FAULT_POINT` 仅用于本地测试，命中时调用 `os._exit(86)`：

- `append_before_commit` / `append_after_commit`
- `bridge_before_commit` / `bridge_after_commit`
- `consume_before_commit` / `consume_after_commit`
- `cleanup_before_commit` / `cleanup_after_commit`
- `load_before_commit` / `load_after_commit`
- `edit_before_commit` / `edit_after_commit`

`EVENTBUS_TEST_BRIDGE_HOLD_MS` 用于在绑定配置后、提交前暂停，以验证在途配置切换拒绝。

这是一个本地确定性验证中间件，不面向公网部署；Flask 接口仅监听本机时使用。
