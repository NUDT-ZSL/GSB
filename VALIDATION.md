# 实际验证记录

验证日期：2026-11-21

## 环境

```text
Python: 3.11.15
SQLite: 3.53.1
NumPy: 2.4.6 (.venv)
固定样本种子: 149
样本规范摘要: bf0da4ad9f04fb41a66e80f47c155e991511f00b6bb6bcbd4f8b8025acdae4c8
```

核心实现与所有验收命令只使用本地文件和 SQLite，不访问网络。

## 单元与集成测试

执行：

```bash
python3.11 -m unittest discover -s tests -v
```

结果：11 项测试全部通过。

覆盖内容：

- 非法通道/分区配置、桥接环、重复事件序号在写库前失败
- 通道追加、级联桥接、预算消费、物理清理和墓碑清理
- 迟到/重复/超水位位点回报不推进或回退订阅位点
- 逻辑时钟回拨与样本摘要错误不改变持久化状态
- 非法 edit 不产生操作状态
- 事件/规则编辑版本与配置快照切换
- 日志损坏可被 `verify` 定位
- 多进程并发追加、桥接与消费无重复 offset/action
- load、publish、bridge、consume、cleanup 各阶段强制终止后 `resume`

## 两次运行逐字节一致

执行：

```bash
PYTHON=python3.11 ./scripts/run_twice.sh
```

最终结果：

```json
{
  "byte_identical": true,
  "ok": true,
  "seed": 149,
  "first": {
    "appended_on_load": 8,
    "transcript_bytes": 57015,
    "transcript_sha256": "d67c9652c8b0720c9edd739660490729a9a4e3750c9449e76a7a48eacb49546f",
    "final_state_digest": "sha256:c8cc01a093333d3b2111c9122503a492c62c9d40c64104c413c36e4334ebdfaf",
    "verify_ok": true
  },
  "second": {
    "appended_on_load": 8,
    "transcript_bytes": 57015,
    "transcript_sha256": "d67c9652c8b0720c9edd739660490729a9a4e3750c9449e76a7a48eacb49546f",
    "final_state_digest": "sha256:c8cc01a093333d3b2111c9122503a492c62c9d40c64104c413c36e4334ebdfaf",
    "verify_ok": true
  }
}
```

两次运行比较的是完整 CLI 输出转录，包括 load、bridge、每轮 status、全部 consume、
cleanup、最终 status、verify 和 resume；长度与 SHA-256 均相同。

## 强制终止恢复

执行：

```bash
python3.11 scripts/crash_recovery.py
```

结果摘要：

```text
load    crash exit 90 -> resume replayed load intent
publish crash exit 90 -> resume replayed publish intent
bridge  crash exit 90 -> resume replayed bridge intent
consume crash exit 90 -> resume replayed consume intent
cleanup crash exit 90 -> resume replayed cleanup intent
verify  ok=true
```

最终验证计数：

```json
{
  "bridge_actions": 17,
  "bridge_states": 3,
  "channels": 3,
  "cleanup_runs": 15,
  "edit_versions": 1,
  "live_records": 5,
  "operations": 17,
  "published_events": 9,
  "records": 16,
  "subscriptions": 3,
  "tombstones": 11,
  "checks": {
    "bridge_integrity": true,
    "cleanup_integrity": true,
    "config_integrity": true,
    "log_integrity": true,
    "output_consistency": true,
    "sample_digest_match": true,
    "subscription_integrity": true
  }
}
```

## 固定种子并发验证

执行：

```bash
.venv/bin/python scripts/concurrency_check.py
```

结果：

```json
{
  "bridge_commands": 3,
  "consume_commands": 15,
  "duplicate_bridge_actions": 0,
  "duplicate_offsets": 0,
  "edit_commands": 2,
  "ledger_offset": 5,
  "ok": true,
  "published_events": 9,
  "publish_commands": 4,
  "rng_backend": "numpy:2.4.6",
  "seed": 149,
  "subscription_offsets": {
    "audit-index": 7,
    "ledger": 5,
    "orders-reader": 6
  }
}
```

`verify` 同时返回 `ok=true`。4 个并发 publish、2 个并发 edit、3 个并发 bridge、
15 个跨 3 个订阅的并发 consume 最终只保留唯一事件 offset、唯一桥接动作和单调
订阅位点。并发锁获取顺序可能改变清理审计行数，但领域终态与订阅位点保持一致。

