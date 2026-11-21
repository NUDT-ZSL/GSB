# 实际验证记录

验证日期：2026-11-22（Asia/Shanghai）

## 环境

```text
Python 3.11.18
SQLite 3.53.1
NumPy 2.4.6
OS: local Linux workspace
Engine runtime dependencies: Python standard library only
```

Python 3.10.12 兼容性也做过一次全量测试；正式验收在项目 `.venv` 的
Python 3.11.18 下执行。

## 测试

命令：

```bash
.venv/bin/python -m unittest discover -v
pyflakes config_engine tests scripts
```

结果：`24` 项测试全部通过，`pyflakes` 无告警。

覆盖：

- 缺失包、畸形 JSON、重复 service/environment、非法副本/超时/重试/相对时间、
  参数声明冲突、分片哈希、代数回退、依赖缺失、依赖环、绝对时间类型。
- 跨代同目标同逻辑时钟时 v2 格式优先级胜出。
- v1 迁移标度、舍入、原始秒数与持久化绝对时间。
- 墓碑、墓碑后新代数重建、过期、未来事件隔离。
- auto 低版本、manual 同代被更高版本 auto 否决。
- 未知事件目标与内容摘要错误不阻塞同流后续事件。
- 合并/事件中途 `SIGKILL` 后 `resume`，视图、水位、迁移与不中断运行一致。
- 4 个进程并行 merge 仅一个操作提交；manual/auto 并行与单命令合并结果一致。
- 重复 merge/reconcile 幂等。
- 篡改视图后 `verify` 检测哈希与重算不一致。

## 固定样本结果

命令：

```bash
python -m config_engine --db state/demo.sqlite --package sample/package.json \
  merge --clock-step t0
python -m config_engine --db state/demo.sqlite --package sample/package.json \
  reconcile --clock-step t3
python -m config_engine --db state/demo.sqlite --package sample/package.json verify
```

`verify` 结果：

```text
view_exists                         passed
published_recovery_bundle_exists    passed
view_recomputes_exactly             passed
stored_view_hash_matches            passed
no_tombstone_in_effective_view      passed
active_params_have_provenance       passed
latest_view_has_state_transitions   passed
recovery_bundle_matches_published_state passed
all_view_hashes_match               passed
event_watermarks_contiguous         passed
accepted_events_have_candidates     passed
event_snapshot_counter_matches      passed
no_half_committed_operations        passed
```

最终状态：

```text
active=4
tombstoned=3
expired=1
pending=0
effective_service_count=4
manual committed_seq=3, watermark_clock_ms=2800
auto   committed_seq=4, watermark_clock_ms=3000
```

## 重复性与恢复

- 两个全新 SQLite 状态库执行相同 `merge -> reconcile -> status -> verify`，
  `merge`、`reconcile`、`status`、`verify` 输出逐位一致。
- 同库重复 merge/reconcile 返回相同操作结果，不新增事件计数。
- 在 `merge:record_migrations` 和 `reconcile:event:auto:2` 后强制退出，
  恢复后的 `view`、`event_streams`、`migration_decisions` 与不中断运行
  逐字段一致。

## 并发

- 4 个进程同时执行同一 merge：全部退出 0，视图版本相同，数据库中只有一个
  committed 合并操作。
- `manual` 与 `auto` 两个 reconcile 并行执行：最终视图与单次全量 reconcile
  相同，双流水位相同，`verify` 通过。

