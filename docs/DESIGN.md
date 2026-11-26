# 设计说明

## 裁决模型

引擎把每个写入事件建模为一组可恢复状态：

- `data_staging`：把确定性载荷写入对象临时文件并 `fsync`。
- `data_rename`：`os.replace()` 原子改名，对象数据提交点。
- `metadata_commit`：在同一个 SQLite 事务中替换完整账本指针。
- `log_append`：追加带 SHA-256 校验和的 JSONL 元数据增补日志并 `fsync`。
- `log_replay`：校验日志前缀、推进回放水位，并幂等重建缓存指针。

`metadata_first` 策略严格按题目第 2 条实现为“临时数据 → 原子改名 → 元数据提交”。
`direct_reversed` 是顺序颠倒对照路径，用于复现元数据已可见但数据缺失的脏缓存。

## 崩溃分类

- `double_success`：数据与元数据均已完成。
- `orphan_object`：完整数据已落盘，但元数据提交前进程终止；或墓碑已提交但数据尚未删除。
- `dirty_cache`：元数据/日志指针已指向新版本，但实际数据缺失或不可读。
- `log_complete`：日志行、换行和校验和完整，可从日志重建。
- `log_half_written`：最后一行截断、无换行或校验和不匹配；恢复时隔离半写尾部并从上一完整水位重放。
- `log_lost`：日志文件缺失或被单点失效注入移走；引擎明确输出丢失结局，再从已验证数据/确定性样本修复。

恢复只扫描 `pending_operations`。已完成事件同时写入 `completed_seqs`、
`event_completions` 和 `object_claims`，重复 worker 不会二次标记同一
`(object_key, content_version, claim_scope)`。

## SQLite 原子账本

每个状态变更在 `BEGIN IMMEDIATE` 事务中：

1. 读取当前 `ledger_pointer` 中的完整 JSON 文档。
2. 在 Python 中应用确定性状态迁移。
3. 向 `ledger_revisions` 插入新版本。
4. 整体替换 `ledger_pointer.document_json`。
5. 提交事务。

`ledger.lock` 用 `fcntl.flock` 串行化多进程事务，SQLite 唯一约束负责最终幂等。
日志回放另有 `metadata-audit.lock` 和 `replay_leases`，避免并发覆盖。

## 时钟与快照

逻辑时钟不读取墙钟。绝对毫秒为：

```text
absolute_ms = anchor_ms + logical_clock * tick_ms
```

v1 的非负相对秒数用 `Decimal` 精确换算到毫秒，采用 floor/向零舍入，并记录亚毫秒余量
（纳秒）。v1/v2 同一对象的裁决元组为
`(cache_generation, decision_clock_ms, format_tie_break)`：缓存代数是 fencing token，
同代再比较绝对逻辑时钟，完全相同时稳定选择 v2。

## 稳定性

- 所有 CLI JSON 使用 `sort_keys=True` 和固定缩进。
- 不使用墙钟、随机 worker ID（CLI 默认 ID 固定）或网络。
- 固定种子为 `20261127`。
- 同一 fixture 的 `load` 标准输出逐字节重复。
