# Verification Record

验证日期：2026-11-24  
工作目录：`/home/zsl/feedback/uid2d1b8ac8-default-341`

## 环境

提供的机器只有 Python 3.10.12，未预装 3.11，且当前账号没有无密码 sudo，因此无法在本机安装 3.11。实现只使用标准库并保持 3.11 兼容；`pyproject.toml` 允许在 3.10 验证机安装运行，目标部署环境仍可直接使用 Python 3.11+。

实际探测：

```text
python: 3.10.12
sqlite: 3.37.2
journal_mode: wal
temp_table: ok
pytest: 8.2.0
```

样本已生成到 `samples/`：

- `samples/v1/snapshot.json`
- `samples/v2/manifest.json`
- `samples/v2/metadata.sqlite`
- `samples/v2/shards/s0001.json`
- `samples/v2/shards/s0002.json`

## 自动化测试

命令：

```bash
PYTHONPATH=. pytest -q
```

实际输出：

```text
......................................                                   [100%]
41 passed in 4.31s
```

覆盖范围：

- v1 重复 ID、邻接不一致、NaN 权重、布尔/负相对秒数、线性索引乱序；
- v2 哈希、分片代数回退、布尔时间戳、节点摘要、跨分片重复 ID、未知墓碑、节点数上下界；
- v1/v2 节点裁决、同逻辑时钟来源优先级、边去重与冲突来源、墓碑审计、v1 时间换算；
- 索引重复构建摘要一致、标签前缀/目标/边标签索引；
- exact/prefix/reach、一跳/两跳、标签与权重过滤、真值比较及首差异；
- 任务数上限、真实内存预算放弃、未启动/运行中/失败/放弃/成功分类；
- 幂等合并、快照冲突、版本回退、时钟回拨、完整替换、增量追加；
- 批次内容篡改拒绝且状态不变、重复提交幂等；
- 真实 `SIGKILL` 后恢复、只补未提交批次、与全新运行逐摘要一致；
- 两个独立遍历进程并发的唯一批次提交；
- 两个并发 `merge` 进程只提交一个快照版本；
- 快照整体替换与旧索引遍历并行时，旧运行保持旧索引读视图；
- 六个 CLI 子命令与稳定排序 JSON。

## 完整验收命令

```bash
python3 scripts/run_acceptance.py --output artifacts/acceptance --force
```

实际输出：

```json
{"budget_abandoned":3,"deterministic_match":true,"log":"/home/zsl/feedback/uid2d1b8ac8-default-341/artifacts/acceptance/verification.log","multi_process_duplicates":0,"ok":true,"resume_watermark":4,"summary":"/home/zsl/feedback/uid2d1b8ac8-default-341/artifacts/acceptance/summary.json"}
```

完整输出在 `artifacts/acceptance/verification.log`，机器可读摘要在
`artifacts/acceptance/summary.json`。

## 预算放弃

预算为 20 个索引条目，4 个起点按每批 1 个、最多 2 个并行任务执行：

```json
{
  "outcomes": {
    "abandoned": 3,
    "not_started": 0,
    "started_failed": 0,
    "started_in_progress": 0,
    "started_succeeded": 1
  },
  "abandoned": [
    {"batch_key": "b0001-bd4d2bedb7d1", "reason": "memory_budget_exhausted: need=11 remaining=8"},
    {"batch_key": "b0002-38503187251a", "reason": "memory_budget_exhausted: need=8 remaining=8"},
    {"batch_key": "b0003-64725d636850", "reason": "memory_budget_exhausted: need=8 remaining=8"}
  ]
}
```

首个批次估计 12 条，启动后剩余 8；第二个批次估计 11，放不下，因此其自身和后续未启动批次全部转为主动放弃。

## SIGKILL 恢复

验收脚本启动真实 CLI 子进程，在首批提交后发送操作系统 `SIGKILL`：

```text
(spawned and will be SIGKILLed after the first committed batch)
SIGKILL pid=368914 run_id=run-25d2039a91abf0ea2c6fbe72
watermark_before_resume: 1
watermark_after_resume: 4
```

恢复结果：

```json
{
  "outcomes_after_resume": {
    "abandoned": 0,
    "not_started": 0,
    "started_failed": 0,
    "started_in_progress": 0,
    "started_succeeded": 4
  },
  "result_digest": "73dce4dd5b309c0e0245c9f79928b7ab776ca37c561bd9918c667b3acf5ec172",
  "truth_digest": "951d4c481a434baf4b8492bbff40ab7b0c6dd21dd82fdbe2df4684957e460523"
}
```

恢复运行的批次摘要与独立全新数据库中的同请求运行逐项一致：

```text
deterministic_match: true
```

## 多进程幂等

两个进程同时执行同一请求：

```json
{
  "watermark": 4,
  "outcomes": {
    "abandoned": 0,
    "not_started": 0,
    "started_failed": 0,
    "started_in_progress": 0,
    "started_succeeded": 4
  },
  "batch_commit_rows": 4,
  "duplicate_batch_rows": 0
}
```

## verify

```json
{
  "check": "sqlite_integrity",
  "integrity_check": "ok",
  "foreign_key_violations": 0
}
{
  "check": "index_rehydrate",
  "ok": true,
  "index_digest": "c137754dd7eefde5778d795a42b668886812feb9abde7e5eb04b92863abdd04c"
}
{
  "check": "source_rebuild",
  "ok": true,
  "index_digest": "c137754dd7eefde5778d795a42b668886812feb9abde7e5eb04b92863abdd04c"
}
{
  "check": "committed_batch_replay",
  "ok": true,
  "verified_batches": 4
}
{
  "check": "final_run_result",
  "ok": true,
  "result_digest": "73dce4dd5b309c0e0245c9f79928b7ab776ca37c561bd9918c667b3acf5ec172"
}
```

## 样本关键结论

- 合并审计节点 11 个，可见节点 10 个，墓碑 1 个（`n3`）。
- 可见边 18 条，索引摘要
  `c137754dd7eefde5778d795a42b668886812feb9abde7e5eb04b92863abdd04c`。
- `n1 --follows--> n2` 在 v1 权重 0.5、v2 权重 0.8；裁决保留两来源并选择 v2 的 0.8。
- `n1` 两跳结果稳定为
  `["n1","n2","n4","n6","n5","n7","n9"]`，墓碑 `n3` 不进入遍历结果。

