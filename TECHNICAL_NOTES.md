# 技术说明与实测记录

## 1. 设计边界

本实现把题述对象落成一个可运行的确定性参考实现：

- v1：单文件 JSON，包含条目和扁平 `replay_order` 映射。
- v2：分片 JSON、`manifest.json` 哈希/摘要清单和 `meta.sqlite` 检查点/墓碑元数据库。
- WAL：按 `generation=<n>` 组织的 JSONL 段，每行携带 SHA-256 内容摘要。
- 崩溃清单：`samples/crashes/crash_plan.json`，定义 merge、replay、checkpoint 三个 SIGKILL 点。
- 运行态：`runtime/logchain.sqlite`、`runtime/locks/`、`runtime/quarantine/`。

初始交付目录中未找到独立种子脚本，因此 `scripts/generate_samples.py` 本身就是固定种子生成器，
种子 ID 为 `logchain-fixed-seed-438`。

## 2. 模块结构

- `logchain/seed.py`：固定种子样本、WAL、孤儿/半写文件和崩溃清单生成。
- `logchain/validation.py`：v1/v2/WAL 解析、校验和业务错误码。
- `logchain/merge.py`：跨版本裁决、v1 时钟换算、统一链和账本构造。
- `logchain/ledger.py`：精确有理数定点换算、余量和误差上界传播。
- `logchain/store.py`：SQLite schema、`BEGIN IMMEDIATE` 事务、物理行表和初始化锁。
- `logchain/engine.py`：原子视图切换、基线/WAL 幂等回放、行版本、检查点时钟闸门。
- `logchain/recovery.py`：孤儿/安全/半写分类、摘要校验、清理隔离和 `resume`。
- `logchain/packages.py`：快照包版本比较、增量/全量/no-op/回退裁决。
- `logchain/diagnostics.py`：稳定 `status`、`load` 和全量重放等价性 `verify`。
- `logchain/cli.py`：`load|merge|replay|resume|status|verify`。
- `tests/test_logchain.py`：9 个本地确定性测试。

## 3. 校验错误码

| 场景 | 业务码 | 诊断字段示例 |
| --- | --- | --- |
| v1 事务序在表内重复 | `V1_DUPLICATE_TXN_SEQ` | `table_name`, `txn_seq` |
| v1 行键在表内重复 | `V1_DUPLICATE_ROW_KEY` | `table_name`, `row_key` |
| 相对秒数不是非负整数 | `V1_INVALID_COMMIT_SECONDS` | `table_name`, `row_key`, `relative_commit_seconds` |
| 回放映射不成立 | `V1_REPLAY_ORDER_MISMATCH` | `expected_keys`, `actual_keys` |
| v2 分片哈希错误 | `V2_SHARD_HASH_MISMATCH` | `path`, `expected_sha256`, `actual_sha256` |
| 日志代数倒退 | `V2_GENERATION_NOT_MONOTONIC` | `table_name`, `row_key`, `generation`, `previous_generation` |
| 提交时刻不是整数 | `V2_INVALID_COMMIT_MS` | `field`, `table_name`, `row_key` |
| 检查点引用缺失 | `V2_CHECKPOINT_REF_MISSING` | `checkpoint_id`, `generation` |
| 检查点代数越界 | `V2_CHECKPOINT_GENERATION_EXCEEDED` | `generation`, `generation_max` |
| WAL 行摘要错误 | `WAL_DIGEST_MISMATCH` | `segment_id`, `line_number`, `table_name`, `row_key` |
| 快照版本回退 | `SNAPSHOT_VERSION_ROLLBACK` | `current_version`, `incoming_version` |

CLI 业务错误以 JSON 写到 stderr；`ChainError` 默认退出码为 `2`，冲突/回退为 `3`。

## 4. 合并与时钟换算

同一条目以 `entry_id` 对齐。裁决键明确为：

```text
(declared_record_clock DESC, source_format_priority DESC, generation DESC)
```

即逻辑时钟优先；相同时钟按 `v2 > v1` 的来源格式优先级；最后用日志代数和稳定排序消除残余歧义。

注入时钟为 `C` 时，v1 的零秒锚点按种子默认时钟平移：

```text
effective_v1_zero_ms = seed.v1_zero_ms + (C - seed.logical_clock_ms)
absolute_commit_ms   = effective_v1_zero_ms + relative_commit_seconds * 1000
```

每次 v1 换算保存：

- `scale=1000`；
- `rounding_mode=nearest`；
- `rounding_direction=exact|nearest_up|nearest_down`；
- `raw_text` / `pre_round_raw_value`；
- `discarded_remainder` 的分子和分母。

迁移决策同时写入 `merge_decisions` 表和 `artifacts/logchain-merge.json`。

## 5. 定点账本与精度敏感

账本不使用二进制浮点。所有原始值、余量和误差上界都用 `fractions.Fraction` 保存，并序列化为
`{numerator, denominator}` 与 `numerator/denominator` 文本。

定点换算：

```text
scaled = floor/ceil/nearest(raw * scale)
discarded = |raw - scaled / scale|
```

保守误差上界：

- floor/ceil：`< 1/scale`；
- nearest：`<= 1/(2*scale)`。

依赖链上的加法把上游累计上界与当前换算上界相加；相邻代数比较把两个累计上界相加。当前种子使用
`scale=100`，g1/g2 实际提交时刻为：

```text
g1 = 1767225603000 ms
g2 = 1767225603001 ms
gap = 1 ms
tolerance = 2 ms
precision_sensitive = true
comparison_error_upper_bound = 9/200 ms = 0.045 ms
```

虽然误差上界很小，但题设规则规定“差值小于容差且裁决依赖差值”即标记敏感；因此状态输出包含完整
`error_source_chain`，恢复裁决固定为按规范代数/事务序重放完整后缀，而不是依赖可能有舍入歧义的
数值先后判断。

## 6. 回放与代数边界

每个逻辑表对应真实 SQLite 物理表 `data__<table>`，包含：

- `row_key`；
- `payload_json`；
- `rv_version`；
- `rv_generation`；
- `rv_tombstone`；
- `source_segment_id`、`txn_seq`、`digest`、`updated_clock_ms`。

段首次处理时先提交 `status='applying'`，随后在单个 `BEGIN IMMEDIATE` 事务中应用全部行并把段标记为
`applied`。SIGKILL 导致未提交行事务由 SQLite 回滚，但 `applying` 持久状态保留，供恢复识别。

`verify` 在临时 SQLite 库中从空库应用同一合并视图和全部 WAL，然后逐表比较完整行字典。比较包含
payload、版本号、来源代数、墓碑、来源段、事务序和摘要，因此不是只比较行数。

## 7. 原子快照替换与并发

快照包处理：

1. 校验清单版本、代数上下界、分片 SHA-256 和集合摘要。
2. 同版本同摘要：`noop`。
3. 低版本：`SNAPSHOT_VERSION_ROLLBACK`。
4. 旧分片全部保留且代数下限不变、仅增加分片：`incremental_append`。
5. 代数下限变化、旧分片删除/修改：`full_replace`。
6. 写入 `chain_stage_*` 临时表，再在一个 SQLite DDL/事务中 `ALTER TABLE ... RENAME` 为版本视图，
   并更新 `pointer_state.current_snapshot_package`。

并发保护：

- `runtime/locks/state-init.lock` 保护 SQLite/WAL 初始化；
- `runtime/locks/snapshot-package.lock` 保护包版本检查和指针切换；
- 每个 WAL 段有独立锁 `runtime/locks/<segment_id>.lock`；
- `applied_segments` 上有 `(generation, version_integer, digest)` 唯一约束；
- SQLite `busy_timeout=30000`、`synchronous=FULL`、WAL journal。

## 8. 崩溃恢复分类

`resume` 先获取 `recovery.lock`，扫描后把对象分类为：

- `safe_referenced_wal` / `safe_committed_log` / `safe_checkpoint`；
- `half_committed_log` / `half_committed_checkpoint`；
- `half_written_file` / `half_written_merge_file` / `half_written_merge_table`；
- `orphan_log`；
- `corrupt_orphan_log`；
- `referenced_content_duplicate`；
- `interrupted_replay_run`。

未被清单引用的 `.jsonl` 孤儿会先逐行校验摘要；校验成功才移动到
`runtime/quarantine/verified-orphans/`。摘要失败的文件移动到 `corrupt-orphans/`，`.partial`
文件移动到 `half-written/`。清单引用路径或内容哈希与引用段相同的文件一律 `preserve`。

恢复依据写入 `recovery_runs.basis_json` 和 `recovery_items`，最新结论通过 `status.recovery`
稳定输出。

## 9. 实测结果

执行环境：

```text
Python 3.11.18
sqlite3 3.53.1
numpy 2.4.6
```

完整测试：

```text
$ python -m unittest discover -s tests -v
Ran 9 tests in ~2.1s
OK
```

完整输出保存在 `artifacts/test-results.txt`。

干净根目录主链路：

```text
load   -> v1=4, v2=5, wal_segments=2, wal_records=8
merge  -> view-2dc00d2cc95426e4
replay -> snapshot baseline + wal-g1-0001 + wal-g2-0002 applied
verify -> ok=true, full_replay_equivalence=true
```

当前摘要：

```text
chain_digest  = 2dc00d2cc95426e49b1b0baa46097d54dafb02fcd62ca577192f91d9b7f993fb
ledger_digest = 1fafba05d896bb964b6ea3884e7edf7cabd11c4af62912e7629adfaa80d7a3a5
```

回放中途 SIGKILL 后实测：

```text
crash exit code = 137
crash state     = wal-g1-0001 status=applying
resume conclusion = RECOVERED_CONSISTENT
verify ok         = true
ledger digest matches normal run
physical rows match normal run exactly
```

示例 JSON：

- `artifacts/example-load.json`
- `artifacts/example-merge.json`
- `artifacts/example-replay.json`
- `artifacts/example-verify.json`
- `artifacts/example-status.json`
- `artifacts/example-recovery.json`
- `artifacts/example-recovered-verify.json`
- `artifacts/example-recovered-status.json`

## 10. 后续可加强项

- 为多表事务加入跨段 transaction ID 原子组；当前种子每代表一个段，段事务已经提供代数边界原子性。
- 若真实 v1/v2 schema 已由外部评测固定，只需在 `validation.py` / `merge.py` 的归一化层调整字段映射，
  状态库、账本、恢复和 CLI 不需重写。
- 可增加 Prometheus/OpenTelemetry 导出，但当前诊断以稳定 JSON 和离线复现为优先。
