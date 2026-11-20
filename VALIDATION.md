# 实际验证结果

验证日期：2026-11-21。所有操作均使用本地 JSON 样本与本地 SQLite 文件。

## 环境

```text
Python 3.11.15
NumPy 2.4.6
pytest 9.2.0
SQLite 3.53.1
```

## 自动测试

```text
$ .venv/bin/python -m pytest -q
..........................                                               [100%]
26 passed
```

覆盖内容：

- 非法拓扑、未知服务、时间倒置、缺失父跨度、父链成环、非法属性和策略回退错误码。
- 非法 `load` 前后 `status` 输出完全一致，确认等待池与持久化状态未改变。
- 根结束、窗口到期、配额耗尽、哈希未命中和策略版本绑定。
- 决策证据、采样率估算、配额账本和 `verify`。
- 决策完成后迟到跨度不翻转结果、不重复扣减配额、重复重放幂等。
- 两种迟到到达顺序得到相同决策、链路摘要和收敛摘要。
- `during_window_close`、`after_quota`、`after_decision_insert` 三种强制退出点的恢复均与基线逐追踪一致。
- 两个并发 `sample` 进程仅产生一组 7 条决策。

## 两次演示一致性

```text
$ .venv/bin/python scripts/demo.py
{
  "database": "artifacts/demo.db",
  "identical": true,
  "run1": "artifacts/demo_run1.jsonl",
  "run1_sha256": "fe929e4597d6b4441ff510083741f5bf9de4886c462b00536204963bbab114c2",
  "run2": "artifacts/demo_run2.jsonl",
  "run2_sha256": "fe929e4597d6b4441ff510083741f5bf9de4886c462b00536204963bbab114c2"
}
```

两次日志均为 58,489 字节且 SHA-256 相同。日志包含 `load`、`sample`、迟到收敛、4 个 `query`、`policy list`、`verify` 和 `status` 的完整 stdout，以及每步退出码。

## 决策与配额

```json
{
  "decisions": {
    "dropped": 2,
    "kept": 5,
    "total": 7
  },
  "late_spans": {
    "affected_decided_traces": 2,
    "count": 2,
    "stored_total": 2
  },
  "quota": {
    "capacity": 2,
    "logical_second": 4,
    "remaining": 2,
    "used": 0
  },
  "staged_events": {
    "pending": 0,
    "processed": 15,
    "total": 15
  },
  "waiting_pool": {
    "high_watermark": 3,
    "spans": 0,
    "traces": 0
  }
}
```

关键区分：

- `trace-slow-003` 为 `DROP / QUOTA_EXHAUSTED`，其目标采样率为 1.0。
- `trace-fast-000` 为 `DROP / SAMPLING_RATE_NOT_SELECTED`，目标采样率为 0.05，稳定哈希值为 0.356041。
- `trace-orphan-005` 为 `KEEP / WINDOW_EXPIRED`。
- `trace-policy-006` 的决策版本为 `policy-v2`。

`verify` 结果：

```json
{
  "error_code": null,
  "ok": true,
  "summary": {
    "checks": 57,
    "decisions": 7,
    "failures": 0,
    "late_spans": 2,
    "quota_seconds": 4
  }
}
```

## 强制终止恢复

```text
$ .venv/bin/python scripts/recovery_demo.py
{
  "baseline_decisions": 7,
  "baseline_verify": true,
  "fault_exit_code": 97,
  "recovered_decisions": 7,
  "recovered_verify": true,
  "resume": {
    "decisions_replayed": 7,
    "events_replayed": 11,
    "recovery_runs": 1
  },
  "state_after_fault": {
    "decisions": 0,
    "quota_used": 0
  },
  "trace_views_identical": true
}
```

强制退出发生在首次保留决策的配额扣减之后、SQLite 提交之前。恢复前决策数与配额用量均为 0；恢复后 7 条决策与无故障基线逐追踪一致，未发生重复配额扣减。

## 复现

```bat
.venv\Scripts\python -m pytest -q
.venv\Scripts\python scripts\demo.py
.venv\Scripts\python scripts\recovery_demo.py
```

