# 实际验证记录

验证日期：2026-11-23

当前执行环境：

```text
Python 3.10.12
pytest 8.3.3
Linux (WSL2 / Ubuntu 22.04)
```

Windows 分支使用 `msvcrt.locking`，并避免依赖 Unix 专属路径语义；本次未在原生 Windows 主机上重复执行。

## 自动化测试

```text
$ python3 -m pytest -q
........................................                                 [100%]
40 passed
```

```text
$ python3 -m pyflakes multipart_store tests examples
pyflakes-ok
```

```text
$ virtualenv /tmp/mps-virtualenv
$ /tmp/mps-virtualenv/bin/python -m pip install --no-build-isolation -e .
$ /tmp/mps-virtualenv/bin/multipart-store --help
venv_exit=0
install_exit=0
entry point: multipart-store {load,upload,complete,abort,resume,status,verify}
```

## 主样本端到端

执行：

```text
load samples/primary.json
upload samples/primary.json
complete --session-id sess-main
complete --session-id sess-checksum      # 预期业务错误
complete --session-id sess-missing       # 预期业务错误
complete --session-id sess-digest        # 预期业务错误
status
verify --session-id sess-main
resume --cleanup
```

关键实际结果：

```text
load: loaded=true, sessions=5, objects=4, events=12
upload: succeeded=12, failed=0, budget_exhausted=false
main completion: status=completed, visible_version=5
main final digest: f380dfc970bac60244e3f606546aca26db8d35582f8fcec49463b56af157c9af
loser: sess-shadow aborted by session:sess-main
checksum failure: E_CHECKSUM_MISMATCH, first_bad_part=0
missing failure: E_PART_MISSING, first_bad_part=1
digest failure: E_DIGEST_MISMATCH, first_bad_part=0
sess-main: phase=completed, visible_version=5, decision_version=5
sess-shadow: phase=aborted, visible_version=3, decision_version=3
orphan parts before cleanup: 4
verify sess-main: valid=true, issues=0
cleanup: deleted=4, gc_log entries=4
```

## 预算样本

```text
budget_units=3
round 1: succeeded=3, abandoned_budget_event_ids=[4, 5]
round 2: succeeded=2, abandoned_budget_event_ids=[]
complete sess-budget: status=completed
verify sess-budget: valid=true
```

## 时钟回拨

```text
event 1: succeeded, visible_version=1
event 2: failed, disposition=not_started, code=E_CLOCK_REGRESSION
session visible_version 保持 1，分片 1 未进入可见集合
```

## 强制终止恢复

分片改名后强杀：

```text
part_after_rename exit=75
status: event 1 status=started, orphan_parts=1
resume --cleanup: reset event 1, deleted orphan=1
后续 round 1 succeeded=3，round 2 succeeded=2
complete: completed，verify=true
```

完成意图后强杀：

```text
completion_after_prepare exit=75
completion_attempts.status=prepared
session phase=pending_completion
resume: replay_completion -> committed
verify=true
```

最终对象落盘后强杀：

```text
completion_after_materialize exit=75
completion_attempts.status=materialized
final object exists=true
resume: replay_completion -> completed
verify=true
```

## 多进程幂等与单完成者

```text
并发同事件 ID、同分片、同 expected_version:
  one success + one idempotent response
  visible_version=1
  events.status=succeeded 数量=1

并发不同内容、同 expected_version:
  one success + one E_VERSION_CONFLICT
  visible_version=1
  可见分片数量=1

并发两个内容合法会话完成同一对象:
  one completed + one aborted
  completed session 数量=1
```
