# 实际验证结果

验证日期：2026-11-23。当前执行环境为 Linux 容器中的 Python 3.10.12；代码只使用 Python 3.10+ 可用语法，同时兼容任务要求的 Python 3.11。PyYAML 6.0.2、SQLite 3.37.2，均由本地安装/标准库提供，未访问网络。

## 1. 自动化测试

```text
$ python -m unittest discover -s tests -v
test_apply_status_verify_and_idempotency ... ok
test_budget_exhaustion_and_operator_abandon ... ok
test_concurrent_apply_has_single_version_and_no_overdraw ... ok
test_crash_after_start_and_after_ledger_rollback_recover ... ok
test_verify_detects_unexplained_drift ... ok
test_version_rollback_is_rejected ... ok
test_window_queue_and_resume ... ok
test_negative_unauthorized_and_quota_samples ... ok
test_provenance_conflicts_approvals_and_quota ... ok
test_duplicate_application_has_distinct_code ... ok
test_invalid_reload_does_not_change_existing_state ... ok
test_reference_type_and_limit_codes_do_not_create_state ... ok

----------------------------------------------------------------------
Ran 12 tests in 3.909s

OK
```

## 2. 主样本完整命令链

执行：

```bash
rm -rf state
python -m mcde load
python -m mcde merge
python -m mcde plan
python -m mcde apply
python -m mcde status
python -m mcde verify
python -m mcde apply
```

关键输出如下（`merge`/`status` 的全量来源链和逐集群字段已省略，实际 JSON 为稳定排序）：

```text
load:
{
  "applications": 3,
  "clock_reset": true,
  "clusters": 8,
  "logical_clock_ms": 1000000,
  "ok": true,
  "regions": 4,
  "snapshot_digest": "19330f5f428c89b44f30fe677ef5e0ad33f0ee2b13ee3e9602af23151bc830cd"
}

merge summary:
{"accepted": 4, "clusters": 8, "rejected": 4, "violations": 5}

east-a / apps.checkout.replicas:
{
  "value": 5,
  "proposed_value": 5,
  "source_layer": "cluster",
  "overridden_layers": ["global", "region"],
  "merge_path": [
    {"layer": "global", "value": 2},
    {"layer": "region", "value": 4},
    {"layer": "cluster", "value": 5}
  ],
  "policy": {
    "allow_diff_overrides": true,
    "approval": {
      "approval_id": "APR-EAST-A-REPLICAS",
      "status": "approved",
      "expires_ms": 1009000
    }
  }
}

rejections:
east-b  E_APPROVAL_EXPIRED
east-c  E_APPROVAL_SCOPE_MISMATCH
west-a  E_OVERRIDE_CONFLICT
north-a E_QUOTA_REPLICAS

plan rounds:
round 1: east-a, south-a  elapsed=60/100
round 2: south-b, west-b elapsed=60/100

apply:
{
  "version": 1,
  "idempotent": false,
  "logical_clock_ms": 1000120,
  "dispatch_status_counts": {
    "applied": 3,
    "failed_after_start": 1,
    "rejected": 4
  }
}

per-cluster dispatch and ledger:
east-a  applied             replicas 9/10 remaining 1; resources 330/800 remaining 470
east-b  rejected            E_APPROVAL_EXPIRED; ledger 0/10, 0/800
east-c  rejected            E_APPROVAL_SCOPE_MISMATCH; ledger 0/10, 0/800
north-a rejected            E_QUOTA_REPLICAS; ledger 0/4, 0/150
south-a applied             replicas 5/12 remaining 7; resources 300/500
south-b applied             replicas 5/12 remaining 7; resources 300/500
west-a  rejected            E_OVERRIDE_CONFLICT; ledger 0/10, 0/600
west-b  failed_after_start  CLUSTER_UNHEALTHY; ledger 0/10, 0/600

status:
{
  "active_version": 1,
  "logical_clock_ms": 1000120,
  "summary": {"applied": 3, "clusters": 8, "conflicts": 5}
}

verify:
{
  "ok": true,
  "residual_count": 75,
  "explained_residual_count": 75,
  "unexplained_drift_count": 0,
  "applied_clean_clusters": 3,
  "summary": {"verdict": "all_residuals_explained"}
}

idempotent replay:
{
  "version": 1,
  "idempotent": true,
  "dispatch_status_counts": {
    "applied": 3,
    "failed_after_start": 1,
    "rejected": 4
  }
}
```

## 3. 解析校验与负样本

```text
$ python -m mcde --db bad.sqlite3 --samples samples/negative/ref_region load
{"ok":false,"error":{"code":"E_REF_REGION", ...}}
db-created: no

$ python -m mcde --db bad.sqlite3 --samples samples/negative/ref_cluster_region load
{"ok":false,"error":{"code":"E_REF_CLUSTER_REGION", ...}}
db-created: no

$ python -m mcde --db bad.sqlite3 --samples samples/negative/ref_cluster load
{"ok":false,"error":{"code":"E_REF_CLUSTER", ...}}
db-created: no

$ python -m mcde --db bad.sqlite3 --samples samples/negative/type_error load
{"ok":false,"error":{"code":"E_TYPE", ...}}
db-created: no

$ python -m mcde --db bad.sqlite3 --samples samples/negative/bad_limit load
{"ok":false,"error":{"code":"E_LIMIT", ...}}
db-created: no
```

越权负样本：

```text
$ python -m mcde --db unauthorized.sqlite3 --samples samples/negative/unauthorized load
exit=0
$ python -m mcde --db unauthorized.sqlite3 --samples samples/negative/unauthorized merge
{"accepted": 3, "clusters": 8, "rejected": 5, "violations": 6}
rejected:
  east-a  E_OVERRIDE_CONFLICT
  east-b  E_OVERRIDE_CONFLICT
  east-c  E_OVERRIDE_CONFLICT
  north-a E_QUOTA_REPLICAS
  west-a  E_OVERRIDE_CONFLICT
```

配额负样本：

```text
$ python -m mcde --db quota.sqlite3 --samples samples/negative/quota load
exit=0
$ python -m mcde --db quota.sqlite3 --samples samples/negative/quota merge
{"accepted": 6, "clusters": 8, "rejected": 2, "violations": 4}
rejected:
  east-a  E_QUOTA_REPLICAS
  north-a E_QUOTA_REPLICAS
```

## 4. 强制终止与恢复

在第一次 `started` 后强制退出：

```text
$ MCDE_CRASH_AT=after_dispatch_start python -m mcde apply
crash_exit=137

$ python -m mcde status
before resume:
  active_version=1
  logical_clock_ms=1000015
  east-a dispatch=started
  east-a replicas_used=0
  south-a/south-b/west-b dispatch=queued

$ python -m mcde resume
resume_exit=0
dispatch_status_counts={"applied":3,"failed_after_start":1,"rejected":4}
east-a dispatch=applied
east-a ledger: replicas 9/10, resources 330/800
approval APR-EAST-A-REPLICAS consumed count=1
cluster_commits(east-a, version=1) count=1

$ python -m mcde verify
{"ok":true,"residual_count":75,"unexplained_drift_count":0}
```

在账本事务内部强制退出：

```text
$ MCDE_CRASH_AT=after_ledger_update python -m mcde apply
crash_exit=137

$ python -m mcde status
before resume:
  east-a dispatch=started
  east-a replicas_used=0
  east-a resources_used=0

$ python -m mcde resume
resume_exit=0
dispatch_status_counts={"applied":3,"failed_after_start":1,"rejected":4}
east-a ledger: replicas 9/10, resources 330/800
```

这说明挂起的账本扣减被 SQLite 完整回滚，而 `started` 水位保留；恢复只补做未完成的完成/提交步骤。

## 5. 多进程并发

三个 `python -m mcde apply` 进程对同一 SQLite 和同一快照并行执行。自动化断言结果：

```text
config_versions count = 1
cluster_commits count = 3
approval_uses(APR-EAST-A-REPLICAS) count = 1
ledger(east-a).replicas_used = 9
ledger rows with used > capacity = 0
```

文件锁和 SQLite `BEGIN IMMEDIATE` 共同保证版本只创建一次、审批只消费一次、同一集群同一版本只提交一次，且账本不会被并发超扣。

## 6. 变更窗口与预算

窗口未开始时：

```text
$ python -m mcde apply
logical_clock_ms=1000000
east-a/south-a/south-b/west-b status=queued
round 1: status=queued, earliest_ms=1000100

$ python -m mcde resume
logical_clock_ms=1000220
east-a/south-a/south-b status=applied
west-b status=failed_after_start
原 queued 轮次 status=window_reopened
```

将 `round_budget_ms` 改为 20（单次完整 dispatch 成本为 30）后：

```text
$ python -m mcde apply
east-a  not_started_budget  ROUND_BUDGET_EXHAUSTED
south-a not_started_budget  ROUND_BUDGET_EXHAUSTED
south-b not_started_budget  ROUND_BUDGET_EXHAUSTED
west-b  not_started_budget  ROUND_BUDGET_EXHAUSTED
ledger east-a replicas_used=0
round 1: status=budget_exhausted, elapsed_ms=0, next_dispatch_cost_ms=30
```

## 7. 回读漂移检测

测试中把 `east-a` 的回读值从 5 改成 8 后：

```text
$ python -m mcde verify
exit=4
{
  "ok": false,
  "unexplained_drift_count": 1,
  "residuals": [
    {
      "cluster": "east-a",
      "key": "apps.checkout.replicas",
      "expected": 5,
      "readback": 8,
      "verdict": "unexplained_drift",
      "basis": ["...snapshot/merge/policy/dispatch/commit..."]
    }
  ]
}
```

正常主样本的所有 75 个残留键均有明确依据链，判定为 `explained_residual`。

