# 实际验证记录

验证时间：2026-11-27（Asia/Shanghai）

环境：WSL Ubuntu 22.04；Python 3.10.12；NumPy 2.0.0；SQLite 3.37.2；pytest 8.2.0。

## 固定样本

命令：

```bash
python3 generate_fixtures.py --output fixtures/seed
```

实际结果：

```json
{"events": 16, "injections": 13, "objects": 6, "ok": true, "seed": 20261127}
```

`load` 校验摘要：

```json
{
  "event_count": 16,
  "injection_case_count": 13,
  "object_count": 6,
  "v1_record_count": 5,
  "v2_record_count": 5,
  "v2_shard_count": 2
}
```

同一时钟序列重复生成的统一快照指纹：

```text
4b4d7adbfb443f2b2c6dd3fd5785b2ed5627859b471908a041d8f3105c3e6cfd
```

`test_load_json_output_is_repeatable` 证明同一 fixture 的两次 CLI `load`
标准输出逐字节相同。

## 自动化测试

命令：

```bash
python3 -m pytest tests/test_diagnostic_engine.py
```

实际结果：

```text
.............                                                            [100%]
13 passed
```

覆盖内容包括：

- 清单键非法/重复、事件序号、负逻辑时钟、v1 秒数、v2 哈希和代数单调性。
- 双路径基线逐键一致。
- 数据先落盘后提交元数据产生孤儿对象并恢复。
- 顺序颠倒产生脏缓存并恢复。
- 日志完整、半写、丢失三类结局。
- 真实 `SIGKILL` 后 CLI resume/verify。
- 4 进程分片并发，16 个对象版本主张且无重复组。
- 比较器首个分歧对象和触发证据链。
- CLI JSON 可重复输出。

## 全注入矩阵

命令：

```bash
bash scripts/check_injection_matrix.sh fixtures/seed
```

实际结果：

```text
PASS INJ-MF-DATA-INTERRUPT
PASS INJ-MF-RENAME-KILL
PASS INJ-MF-META-KILL
PASS INJ-MF-META-ROLLBACK
PASS INJ-REV-META-KILL
PASS INJ-REV-META-INTERRUPT
PASS INJ-LOG-APPEND-HALF
PASS INJ-LOG-APPEND-KILL
PASS INJ-LOG-DATA-KILL
PASS INJ-LOG-REPLAY-KILL
PASS INJ-LOG-REPLAY-INTERRUPT
PASS INJ-LOG-REPLAY-ROLLBACK
PASS INJ-LOG-REPLAY-LOST
MATRIX_OK 13/13
```

安全注入命令按设计返回业务错误 JSON 和退出码 `2`；随后 `resume` 返回 `ok: true`，
SQLite invariant 检查通过。

## 端到端验收

命令：

```bash
bash scripts/acceptance.sh
```

实际结果：

```text
ACCEPTANCE_ASSERTIONS_PASSED
baseline_consistent=true
hard_orphan_recovered=true
complete_log_dirty_recovered=true
parallel_claims=16
ACCEPTANCE_OK /tmp/diag-acceptance-XAnRvy
```

脚本中两次 `--hard-kill` 均由操作系统报告 `Killed`，shell 观察退出状态 `137`：

- `INJ-MF-RENAME-KILL`：恢复裁决为 `orphan_object`，元数据前滚后缓存 clean。
- `INJ-LOG-APPEND-KILL`：完整日志回放重建指针；数据缺失窗口裁决为 `dirty_cache`，补写后 clean。

## 并发竞争补充检查

额外手动执行过两个全量 `write_log` worker 竞争同一状态目录；临时文件按 worker 隔离后，
两个 CLI 均退出 `0`，`verify` 返回 `ok=true`，`object_claims` 仍为 16 行且无重复主张。
