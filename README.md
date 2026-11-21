# WAL Replay and Checkpoint Recovery

一个仅依赖 Python 标准库（`sqlite3`、`hashlib`、`json`）的本地确定性 WAL
回放组件。它覆盖 WAL 链校验、v1/v2 检查点解析、并发预算调度、逻辑时钟注入、
检查点整体替换、崩溃恢复、多进程幂等和稳定 JSON 证据输出。

运行时不需要访问网络，不读取系统墙钟来决定回放完成时刻、检查点节奏或预算。

## 环境与安装

- 推荐 Python 3.11+；当前实现同时兼容 Python 3.10。
- SQLite 使用 Python 标准库 `sqlite3`。
- 测试依赖 `pytest`；环境准备要求中的 `numpy` 可一并安装，但组件本身不依赖
  `numpy`。

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
python -m pip install -e .
python -m pip install pytest numpy
```

也可以不安装包，直接在仓库根目录运行 `python -m walrec ...`。

## 目录

```text
walrec/                 核心实现
  wal.py                WAL JSONL 发现、结构与链校验
  checkpoint_io.py      v1/v2 检查点解析、哈希校验与 v2 包写入
  store.py              SQLite 状态、文件锁、原子指针替换
  engine.py             调度、预算、逻辑时钟、恢复、检查点安装
  cli.py                load/apply/checkpoint/resume/status/verify
samples/normal/         正常链，含 v1、v2 与策略
samples/budget/         预算受限链
samples/abnormal/       断链、摘要、缺代、回放代回退、版本回退变体
tests/                  23 项 pytest 验收测试
scripts/generate_samples.py
scripts/run_validation.py
artifacts/validation-report.json
```

重新生成确定性样本：

```bash
python scripts/generate_samples.py
```

## 快速运行

```bash
# 从 v1 空检查点全量回放
python -m walrec load --sample samples/normal --db work/full.db \
  --checkpoint-gen 1 --reset
python -m walrec apply --sample samples/normal --db work/full.db
python -m walrec status --sample samples/normal --db work/full.db
python -m walrec verify --sample samples/normal --db work/full.db

# 从 v2 快照只回放增量
python -m walrec load --sample samples/normal --db work/inc.db --reset
python -m walrec apply --sample samples/normal --db work/inc.db

# 恢复强制终止留下的状态
python -m walrec resume --sample samples/normal --db work/full.db

# 创建 v2 检查点；或安装外部 v2 包
python -m walrec checkpoint --sample samples/normal --db work/inc.db \
  --clock-ms 1700
python -m walrec checkpoint --db work/inc.db \
  --package path/to/gen-000006
```

所有命令输出按 key 排序的稳定 JSON。`verify` 在存在不一致时退出码为 1；
业务错误退出码为 2；预算/失败导致未完成时 `apply`/`resume` 退出码为 3。

## 样本格式

### WAL

文件位于 `wal/wal-0001.jsonl`、`wal-0002.jsonl` 等。段号必须从 1 连续，
文件内 `segment_no` 必须与文件名一致。每一行是一条单行操作事务记录：

```json
{
  "segment_no": 1,
  "logical_tx_id": 4,
  "prev_tx_id": 3,
  "prev_tx_hash": "<sha256>",
  "checkpoint_gen": 1,
  "table": "accounts",
  "row_key": "alice",
  "operation": "UPDATE",
  "before": {"balance": 100},
  "after": {"balance": 110},
  "duration_ms": 5,
  "content_hash": "<sha256>",
  "tx_hash": "<sha256>"
}
```

- `operation` 只允许 `INSERT`、`UPDATE`、`DELETE`。
- `INSERT` 必须有 `before=null` 和非空对象 `after`；`UPDATE` 必须同时有
  对象 `before`/`after`；`DELETE` 必须有对象 `before` 和 `after=null`。
- `content_hash` 是
  `sha256(canonical_json({table,row_key,operation,before,after}))`。
- `tx_hash` 是记录除 `tx_hash` 外全部字段的规范 JSON SHA-256。
- `prev_tx_id`/`prev_tx_hash` 必须指向前一条记录；首条可以从零哈希开始，
  也可以精确锚定到某个检查点的水位与 `chain_hash`。因此 WAL 可以只包含
  检查点之后的增量事务。
- 事务号必须严格递增。样本中的事务号连续，因此水位可直接作为已应用边界。

规范 JSON 使用 UTF-8、key 排序、紧凑分隔符，并拒绝 NaN/Infinity，保证跨进程
与跨平台结果稳定。

### v1 检查点

`checkpoints/v1/checkpoint-v1.json`：

```json
{
  "format": "checkpoint-v1",
  "checkpoint_gen": 1,
  "manifest_version": 1,
  "applied_watermark": 0,
  "chain_hash": "0000...",
  "tables": {"accounts": 0, "ledger": 0},
  "logical_time": {"kind": "relative", "value_ms": 250},
  "conversion": {
    "base_logical_ms": 1000,
    "scale": "1.5",
    "rounding": "half_up"
  },
  "summary_hash": "<sha256>"
}
```

绝对逻辑时刻按下式换算：

```text
absolute = base_logical_ms + round(relative_ms * scale, rounding)
```

`rounding` 允许 `half_up`、`floor`、`ceil`。加载后 `status.checkpoint.conversion`
会记录原始相对值、基数、十进制 scale、舍入方向和绝对毫秒值。v1 只有行数摘要，
加载时会从空库按 WAL 重放到水位；若只有检查点之后的增量 WAL，则应使用 v2。

### v2 检查点包

```text
checkpoints/v2/gen-000002/
  manifest.json
  meta.sqlite
  shards/<table>-<hash>.json
```

`manifest.json` 关键字段：

```json
{
  "format": "checkpoint-v2",
  "checkpoint_gen": 2,
  "manifest_version": 2,
  "parent_checkpoint_gen": 1,
  "replay_generation": 2,
  "min_replay_generation": 1,
  "applied_watermark": 5,
  "chain_hash": "<sha256>",
  "commit_ts_ms": 1500,
  "logical_clock_ms": 1500,
  "update_kind": "full",
  "table_summaries": {
    "accounts": {
      "live_row_count": 2,
      "max_version": 2,
      "row_count": 2,
      "version_digest": "<sha256>"
    }
  },
  "dirty_pages": {
    "accounts": {"p0123": ["alice", "bob"]}
  },
  "shards": [
    {
      "table": "accounts",
      "file": "shards/accounts-<hash>.json",
      "row_count": 2,
      "sha256": "<shard-file-sha256>"
    }
  ]
}
```

分片保存带 `version`、`source_tx`、`tombstone`、`row_hash` 的行。`meta.sqlite`
的 `checkpoint_meta` 单行指针保存 manifest 规范哈希及代数/版本字段。解析时会
校验分片字节哈希、行哈希、表版本摘要、清单与元数据库一致性。v2 代按
`checkpoint_gen` 升序检查 `replay_generation` 单调、`commit_ts_ms` 单调和
`manifest_version` 单调。

### 策略

`policy.json`：

```json
{
  "max_concurrent_txs": 3,
  "round_budget_ms": 24,
  "checkpoint_interval_ms": 30,
  "clock_step_ms": 8,
  "clock_tolerance_ms": 4,
  "inject_failure_tx_ids": [],
  "fail_round_on_error": true
}
```

调度器在每轮按 WAL 顺序做确定性准入：并发数不超过上限，已准入事务
`duration_ms` 之和不超过轮预算，遇到同一 `(table,row_key)` 冲突时把后续事务
推迟到下一轮。准入事务以同一逻辑轮起点并行提交；SQLite 和跨进程文件锁把
实际写提交串行化，保证不同进程看到一致顺序。已启动事务不会因预算而截断。

结局状态：

- `applied`：已原子提交。
- `duplicate`：其他进程或前一实例已提交，本次幂等跳过。
- `failed_after_start`：已登记在途后失败。
- `not_started_budget`：本轮预算不足，未启动。
- `actively_abandoned`：因已启动失败、达到最大轮数等原因主动放弃本轮剩余事务。

## 状态库与恢复

SQLite 状态库使用 WAL 日志模式和 `BEGIN IMMEDIATE` 写事务：

- `data_rows`：逻辑表、行键、应用后的 canonical payload、版本、来源事务、
  删除墓碑和行哈希。
- `applied_txs`：权威已提交事务集合。
- `in_flight`：在途事务登记；数据事务与 `applied_txs` 在同一个 SQLite 事务
  中提交，崩溃后不会半提交。
- `checkpoint_pointer`：当前指针。
- `checkpoint_history`：已提交检查点历史，`checkpoint_gen` 唯一。
- `tx_outcomes`：每轮每事务结局与逻辑起止时刻。

水位不是盲目取最大事务号，而是从 1 开始扫描连续已提交集合重建，因此可修复
“已提交事务存在但水位落后”。恢复还会：

- 清理未提交的在途登记；
- 用 WAL 前缀重建并回滚孤立的半应用行；
- 重算表摘要和状态摘要；
- 只回放没有出现在 `applied_txs` 中的事务。

### 强制终止测试钩子

以下环境变量仅用于故障注入，不参与正常语义：

```text
WALREC_FAILPOINT=after_inflight_before_data
WALREC_FAILPOINT=after_data_before_commit
WALREC_FAILPOINT=after_commit
WALREC_FAILPOINT=checkpoint_after_package
WALREC_FAIL_TXID=<可选事务号>
```

`after_data_before_commit` 使用 `os._exit(91)` 模拟无退出报告的强制终止；
SQLite 回滚未提交数据，已单独提交的在途登记保留，`resume` 可识别并继续。
`checkpoint_after_package` 在包写完但指针未提交时终止，重试会复用完整包并
只提交一次指针。

## 检查点提交与版本裁决

新检查点按当前已应用水位生成 v2 包，先写临时目录并整体重命名，再在一个
SQLite 事务中通过临时表 `_checkpoint_pointer_stage` 替换单行当前指针并追加
历史。旧包不会被覆盖。

安装外部包时按以下顺序裁决：

1. 相同代数与相同 manifest 哈希：`noop`。
2. 代数、manifest 版本或回放代数回退：`E_VERSION_ROLLBACK`。
3. 同代但 manifest 不同：`E_VERSION_ROLLBACK`。
4. 表版本摘要与当前持久化行不一致：`E_SUMMARY_MISMATCH`。
5. `update_kind=delta` 且父代不是当前指针：`E_VERSION_ROLLBACK`。
6. `update_kind=delta`：`incremental_append`。
7. `update_kind=full`：`full_replace`。

任何拒绝都发生在写事务回滚中，当前指针和旧检查点文件保持不变。

## 错误码

| 错误码 | 含义 |
| --- | --- |
| `E_BAD_WAL` | JSONL 结构、枚举或前后镜像非法 |
| `E_BAD_CHECKPOINT` | 检查点结构/元数据非法 |
| `E_CHAIN_BREAK` | 事务号顺序、前序事务或前序哈希断链 |
| `E_SUMMARY_MISMATCH` | 行内容、事务、清单或分片摘要不匹配 |
| `E_CHECKPOINT_MISSING` | WAL 依赖的检查点代数不存在 |
| `E_REPLAY_GENERATION` | v2 回放代数不单调 |
| `E_CLOCK_ROLLBACK` | 逻辑时钟回拨超过容差 |
| `E_VERSION_ROLLBACK` | 检查点版本、代数或回放代回退 |
| `E_APPLY_CONFLICT` | 当前行与 UPDATE/DELETE 前镜像冲突 |
| `E_APPLY_FAILED` | 启动后的应用失败 |
| `E_HALF_APPLIED` | 无法定位半应用行对应的 WAL 事务 |
| `E_POLICY` | 策略配置非法 |
| `E_STATE` | 状态库未初始化、已加载或状态不合法 |

所有校验错误都在打开或修改状态库之前返回；`load` 遇到非法样本不会创建
SQLite 文件。

## 验证

运行全部测试：

```bash
python -m pytest -q
```

生成实际验证报告：

```bash
python scripts/run_validation.py
```

当前验证结果：**23 passed**，覆盖：

- 5 类非法样本分别返回 `E_BAD_WAL`、`E_CHAIN_BREAK`、
  `E_SUMMARY_MISMATCH`、`E_CHECKPOINT_MISSING`、`E_REPLAY_GENERATION`，
  且不创建状态库；
- v1 全量回放与 v2 快照增量回放逐行逐值一致；
- 注入差异行后比对工具报告差异事务号；
- 轮预算放弃、无事务可准入、启动后失败、主动放弃三类结局；
- v1 相对时刻换算标度/舍入方向、逻辑时钟回拨拦截；
- 检查点 full/delta/noop 裁决、版本回退拒绝、旧包不变；
- `os._exit(91)` 回放中断恢复、检查点包写入中断重用、陈旧水位与半应用行修复；
- 4 个进程并发应用同一 WAL，最终每个事务只应用一次；
- 重复 apply 不改变行版本或检查点指针；
- 两个全新数据库的 `load`/`apply`/`status` 输出逐字节一致；
- `verify` 定位篡改行的来源事务号。

详细机器可读结果见 `artifacts/validation-report.json`。
