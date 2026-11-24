# 实际验证记录

## 环境

```text
Python 3.10.12
pip 26.3
SQLite 3.37.2
pytest 8.2.0
Linux/WSL2 x86_64
```

任务描述目标环境是 Windows + Python 3.11。本次执行容器为 Linux/WSL2 +
Python 3.10.12；工程只使用标准库并保持跨平台路径与锁实现。

## 测试收集

```text
$ python -m pytest --collect-only -q
32 tests collected in 0.01s
```

## 完整测试

```text
$ python -m pytest -q
................................ [100%]
32 passed in 2.26s
```

覆盖点：

- v1/v2/补丁/告警严格解析与逐记录拒绝
- v1 观测时钟换算、v2 代数链、墓碑覆盖与跨代冲突
- 版本重叠带 `E_VERSION_AMBIGUOUS`
- 固定路径与 float64 路径 `E_WINDOW_AMBIGUOUS`
- 进程谱系环 `E_PROC_CYCLE`
- 告警到达顺序无关、补丁流乱序无关
- 1 线程/4 线程结论一致
- SQLite 补丁事务回滚与重复批次
- 墙钟超时、告警预算、确定性队列准入
- 检查点恢复、损坏快照回退、结构版本重建、`E_LOCK`

## 固定样本生成

```text
$ python tools/generate_sample.py --out samples
generated samples seed=20261125 observation_clock_ms=1700000000000
```

生成文件：

- `samples/alerts.jsonl`
- `samples/rules_v1.json`
- `samples/v2_shards/v2-000.json`
- `samples/v2_shards/v2-001.json`
- `samples/v2_meta.sqlite`
- `samples/patches.jsonl`
- `samples/truth_sync.json`

补丁到达顺序：`P200, P100, P300, P150`

补丁逻辑顺序：`P100, P150, P200, P300`

## 端到端校验

```text
$ python -m alert_engine verify \
    --sample samples \
    --workspace build/engine-verify-1 \
    --workers 4 \
    --queue-limit 64

{
  "failures": [],
  "ok": true,
  "status": "OK"
}
```

固定样本运行结论：

```text
alerts_accepted          24
alerts_rejected           6
alerts_bound             22
alerts_version_ambiguous  1
clusters                 15
rule_versions            17
rule_conflicts            7
window_ambiguities        2
proc_cycles               1
recompute_full_match   true
```

出现的业务码：

```text
E_ALERT_ID_DUP
E_ALERT_TS
E_DIGEST
E_JSON
E_PROC_CYCLE
E_RULE_CONFLICT
E_RULE_INACTIVE
E_RULE_UNKNOWN
E_SEVERITY
E_VERSION_AMBIGUOUS
E_WINDOW_AMBIGUOUS
```

## 并发一致性

自动化测试分别以 `max_workers=1` 与 `max_workers=4` 从零运行完整样本，比较：

- 规则视图
- 全部告警绑定
- 全部事件簇（含稳定簇 ID、代表与成员）
- 拒绝清单
- 三类歧义/环诊断

结果完全一致。

## 补丁乱序一致性

将 `patches.jsonl` 行序反转后重新运行。增量路径产生了不同的中间失效顺序，但最终
规则视图、绑定、簇、拒绝与歧义清单与原始顺序完全一致，且
`diagnostics.recompute.full_match=true`。

## 检查点恢复

```text
$ python -m alert_engine run \
    --sample samples \
    --workspace build/engine-verify-1 \
    --workers 4 \
    --queue-limit 64 \
    --resume

checkpoint.restored = true
checkpoint.load_residuals = []
counts.patch_batches_applied = 4
diagnostics.recompute.full_match = true
status = OK
```

自动化测试还覆盖：

- 将最新快照内容截断为不可解析 JSON，恢复时回退到前一完整检查点并报告
  `E_CHECKPOINT_CORRUPT` 残差；
- 将最新 SQLite 行结构版本改为 999，恢复要求整体重建；
- 检查点临时目录遗留 `.tmp` 文件时，在恢复结果中列为孤立临时文件；
- 持有 `.engine.lock` 时写报告稳定返回 `E_LOCK`。

## 资源预算

自动化测试使用同一固定样本验证：

- `max_alerts_per_host=1` 时所有主机返回 `E_BUDGET`，不产生簇；
- `per_host_timeout_ms=0` 时所有主机按真实墙钟立即超时并返回 `E_TIMEOUT`；
- `max_workers=1, pending_host_queue_limit=1` 时准入容量为 2，其余主机稳定返回
  `E_QUEUE_FULL`。

## 强制终止边界

由于无法捕获 `SIGKILL`，运行被强制终止时不保证生成退出报告。完全写入的旧报告
不会被半写文件覆盖；已提交 SQLite 行、孤立快照和临时文件可通过下次恢复审计识别。
