# 实际验证记录

环境：Linux 工作区，Python 3.10.12，SQLite 3.37.2，Flask 3.0.3，pytest 8.3.3。
实现兼容目标环境 Python 3.11+；未使用 Docker、在线服务或外部附件。

## 自动化测试

命令：

```bash
pytest -q
```

实际结果：

```text
.............................                                            [100%]
29 passed in 4.90s
```

覆盖点包括：

- v1/v2 合并裁决、时间换算、兼容降级。
- 字段个数、重复序号、桥接环、分片哈希、v2 信封校验和错误码。
- 确定性分区、连续偏移、批次切分、重复提交幂等/冲突。
- 桥接过滤与变换、代数不匹配、上游水位不足、位点回退。
- 订阅偏移边界、重复位点回报、并发消费不超进。
- 物理清理与墓碑标记、逻辑时钟回拨拒绝。
- 事件新增/修改/删除、配置版本切回、事件载荷篡改检测。
- 8 个并发生产者、6 个并发相同位点回报、4 个并发消费者。
- 追加/桥接/消费/清理在提交前后强杀后的恢复。
- 转发开始后配置版本变化的 `CONFIG_CHANGED` 拒绝。

多进程测试连续运行通过，未使用 mock 替代 SQLite/文件锁。

## 端到端演示

命令：

```bash
python scripts/generate_samples.py
python scripts/run_demo.py
```

实际关键输出：

### 加载与合并

```json
{
  "bridges": 1,
  "channels": 3,
  "config_version": 1,
  "degradations": 6,
  "events": 8,
  "idempotent": false,
  "ok": true,
  "subscriptions": 3
}
```

合并裁决样例：

```text
orders/1  logical_clock tie; generation 2 > 1        -> v2
orders/2  logical_clock 115 > 110                    -> v2
orders/3  only source v1 at generation 1             -> v1 + v2 默认字段降级
orders/5  logical_clock 150 > 145                    -> v1
audit/1   logical_clock 60 > 50                      -> v2
```

初始水位：

```text
orders: high=7, bridge next=0, lag=7
audit:  high=1
metrics high=0
```

### 桥接、消费、清理

```json
{
  "bridge_summary": {
    "delivered": 4,
    "filtered": 3,
    "final_offset": 7
  },
  "consume": {
    "subscription": "orders-reader",
    "from_offset": 0,
    "offset": 7,
    "event_count": 7
  },
  "cleanup": {
    "physical_removed": 6,
    "tombstones_marked": 1,
    "bytes_before": 5838,
    "bytes_after": 3437,
    "blocked_slowest": {
      "channel": "audit",
      "kind": "subscription",
      "name": "audit-reader",
      "offset": 0,
      "reason": "unconsumed_reader"
    },
    "reason": "retention_bytes_exceeded"
  }
}
```

桥接输出中 4 条投递订单为：

```text
O-1001 amount 11.55
O-1003 amount 25.58
O-1004 amount 16.50
O-1005 amount 33.00
```

`O-1002`、`O-1006` 和墓碑事件 `O-1007` 被过滤。

清理后 `verify`：

```json
{
  "ok": true,
  "errors": [],
  "checks": {
    "bridges": {"orders-to-audit": {"checks": 7, "next_offset": 7}},
    "channels": {
      "orders": {"low_watermark": 7, "high_watermark": 7, "gaps": 0},
      "audit": {"low_watermark": 0, "high_watermark": 5, "gaps": 0}
    },
    "pending_bridge_runs": 0,
    "pending_operations": 0
  }
}
```

### 编辑与追加

```json
{
  "config_version": 2,
  "parent_version": 1,
  "patch_id": "toggle-audit-tag",
  "operations": {
    "events": {"add": 0, "update": 0, "delete": 0},
    "bridges": {"add": 0, "update": 1, "delete": 0},
    "subscriptions": {"add": 1, "update": 0, "delete": 0}
  }
}
```

追加事件：

```json
{
  "appended": 1,
  "channel": "orders",
  "config_version": 2,
  "results": [{
    "appended": true,
    "sequence": 8,
    "offset": 7,
    "logical_clock": 180
  }]
}
```

### 强制终止恢复

故障点：

```text
EVENTBUS_FAULT_POINT=bridge_before_commit
```

子进程结果：

```text
process_exit=86
```

`resume` 结果摘要：

```json
{
  "ok": true,
  "recovered_operations": [{"kind": "bridge-cycle", "status": "completed"}],
  "recovered": [{"bridge_id": "orders-to-audit", "status": "completed"}],
  "channels": {
    "orders": {"low": 0, "high": 7},
    "audit": {"low": 0, "high": 5}
  },
  "remaining_running": 0,
  "remaining_pending_operations": 0
}
```

恢复后：

```json
{
  "ok": true,
  "checks": {
    "bridges": {"orders-to-audit": {"checks": 7, "next_offset": 7}},
    "pending_bridge_runs": 0,
    "pending_operations": 0
  }
}
```

### 并发幂等

4 个生产者进程同时向同一 `orders` 通道追加互不相同的单事件：

```json
{
  "exit_codes": [0, 0, 0, 0],
  "orders_high_watermark": 11,
  "verify": true
}
```

初始 7 个事件加 4 个并发追加后 high watermark 为 `11`，无重复偏移、无跳号。

## 复现

```bash
python scripts/generate_samples.py
pytest -q
python scripts/run_demo.py
```

演示数据库位于 `data/demo*.sqlite`，每次运行会先安全清理并重建。输出最后一行应为：

```text
DEMO OK
```
