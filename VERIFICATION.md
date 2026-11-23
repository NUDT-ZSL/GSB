# Verification

验证日期：2026-11-24

## 环境

```text
Python 3.10.12
SQLite 3.37.2
NumPy 1.26.4
Linux/WSL; Windows 使用同一标准库路径与 msvcrt 文件锁分支
```

## 自动化测试

```bash
python -m unittest discover -s tests -v
```

结果：

```text
Ran 22 tests in 2.219s
OK
```

## 命令行验收片段

加载样本后基线为：

```json
{
  "watermark": 14,
  "checkpoint_generation": 2,
  "checkpoint_source_format": "v2"
}
```

首次增量回放：

```json
{
  "applied_txns": [15, 16, 17, 18, 19, 20, 21, 22],
  "watermark": 22
}
```

重复回放：

```json
{"applied_txns": [], "watermark": 22}
```

验证：

```json
{
  "matched": true,
  "row_count": 9,
  "actual_row_count": 9,
  "incremental_row_count": 9,
  "full_replay_row_count": 9,
  "first_difference": null
}
```

编辑事务 18 后，失效闭包包含：

```text
row_version accounts/acct-frank txn=18 version=2
row_version accounts/acct-frank txn=22 version=3
checkpoint_refs: []
delay_records: txn 18 reuse, txn 22 reuse
```

编辑事务 12 后：

```text
checkpoint_refs: generation 2 invalidate
rebuild.baseline_generation: 1
apply 从 txn 9 开始重建
txn 15..22 延迟原记录复用
verify.matched: true
```

检查点：

```text
checkpoint create -> decision=created, generation=3
checkpoint receive same package -> decision=noop
checkpoint receive older sample package -> CHECKPOINT_ROLLBACK (exit 2)
cross-state receive -> incremental_append
full package receive -> full_replace
```

强制终止：

```text
apply_after_rows @ txn 18 -> exit 75
恢复前: watermark=17, pending_transactions=[18], pending_operations=1
resume -> watermark=22, delay records=8
最终 _rows 与不中断运行的 control DB 逐行一致
```

并发：

```text
4 个并行 apply 进程 -> 全部 exit 0
最终 watermark=22, applied_transactions=22, delay records=8
```

错误码验证：

```text
非法操作枚举      -> WAL_INVALID_ROW
跨段事务号重叠    -> WAL_CHAIN_BROKEN
WAL 内容被篡改    -> WAL_HASH_MISMATCH
依赖代数缺失      -> WAL_CHECKPOINT_DEPENDENCY
v2 分片被篡改     -> CHECKPOINT_HASH_MISMATCH
v2 回放代数回退   -> CHECKPOINT_CHAIN_BROKEN
逻辑时钟回拨      -> CLOCK_ROLLBACK
```

## 样本确定性

对 `samples/` 下全部 11 个文件计算 SHA-256，重新执行
`python tools/generate_samples.py` 后再次计算，两次清单纯差异为空。
