# 分层存储配额与限流账本

一个无服务端依赖的本地 Python 组件：在可注入逻辑时钟下，对租户、桶、前缀三层存储配额与租户级令牌桶限流进行确定性裁决，并使用 SQLite 单事务持久化全部账目。

## 交付内容

- 可集成库：`quota_ledger.QuotaLedger`
- CLI：`load`、`write`、`delete`、`resume`、`status`、`verify`
- 固定样本：三层配额、事件流、限流配置、逻辑时钟清单
- 双运行演示：`scripts/demo.py`
- 单元与多进程测试：`tests/`
- 设计说明：`docs/DESIGN.md`
- 实际验收记录：`docs/ACCEPTANCE.md`、`artifacts/demo/`

## 环境

生产和目标环境为 Python 3.11/3.12；本次执行环境为 Python 3.10.12，代码保持兼容 Python 3.10+。

```text
Python       3.10.12
SQLite       3.37.2（Python 标准库 sqlite3）
NumPy        1.26.4
```

Windows 初始化示例：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m unittest discover -s tests -t . -v
python scripts/demo.py
```

SQLite 文件、WAL/SHM 文件、锁文件和演示临时目录均位于工作区 `artifacts/` 下。

## 快速开始

```bash
python -m quota_ledger load \
  --db artifacts/quickstart/ledger.db \
  --quotas samples/quotas.json \
  --limiter samples/limiter.json

python -m quota_ledger write \
  --db artifacts/quickstart/ledger.db \
  --events samples/events.json

python -m quota_ledger status --db artifacts/quickstart/ledger.db
python -m quota_ledger verify --db artifacts/quickstart/ledger.db
python -m quota_ledger resume --db artifacts/quickstart/ledger.db
```

所有 CLI JSON 输出均使用 `sort_keys=True` 和紧凑分隔符，相同账目状态输出逐字节一致。

## 库接口

```python
from quota_ledger import QuotaLedger
from quota_ledger.utils import read_json

quota = read_json("samples/quotas.json")
limiter = read_json("samples/limiter.json")
ledger = QuotaLedger.load("artifacts/library/ledger.db", quota, limiter)

result = ledger.write({
    "seq": 1,
    "tenant": "demo",
    "bucket": "media",
    "prefix": "alpha",
    "object_key": "a1",
    "bytes": 150,
    "deleted": False,
    "clock_ms": 0,
    "idempotency_key": "demo-1",
})

status = ledger.status()
verification = ledger.verify()
recovery = ledger.resume()
```

主要方法：

- `QuotaLedger.load(db_path, quota_document, limiter_document)`
- `QuotaLedger.open(db_path)`
- `write(event, fault_stage=None, crash_stage=None)`
- `delete(event, ...)`
- `write_batch(events, ...)`
- `status()`
- `verify(source_events=None)`
- `resume()`

`fault_stage` 与 `crash_stage` 是测试钩子；生产调用无需传入。

## 裁决语义

### 分层配额

- 三层路径为 `tenant` -> `bucket` -> `prefix`。
- 每次写入同时更新三层用量，父层用量始终覆盖其全部子层用量。
- 检查顺序固定为：限流，然后 `tenant.bytes`、`tenant.objects`、`bucket.bytes`、`bucket.objects`、`prefix.bytes`、`prefix.objects`。
- 任一层硬阈值判断为 `projected > floor(capacity * hard_pct / 100)`，命中后拒绝并返回第一个违反项及全部层检查依据。
- 软阈值判断为 `projected > floor(capacity * soft_pct / 100)`。软超限允许写入，并只在状态从正常进入软限时生成一次 `ENTER` 告警。
- 已处于软限的层重复写入不会重复告警；用量回到软阈值或以下时生成一次 `EXIT` 告警。
- 删除按 `(tenant, bucket, prefix, object_key)` 找回持久化对象大小，精确归还三层空间和对象数。
- 同键覆盖写按大小差值核算；对象数不变。
- 删除不存在的对象返回可幂等记录的 `E_OBJECT_NOT_FOUND` 拒绝裁决，不消耗令牌、不改配额。

### 令牌桶

- 令牌使用整数微令牌（1 token = `1_000_000`）和精确有理数余数，不使用浮点漂移，不读取系统墙钟。
- 有效补充速率为 `min(refill_rate_per_second, requests_per_second)`。
- 有效突发容量为 `bucket_capacity + burst_depth`，初始化时满桶。
- 每次允许的写入（含删除）消耗 1 token。
- 令牌不足时拒绝，返回精确 `wait_ms` 与 `max(wait_ms, backoff_window_ms)` 的 `retry_after_ms`。
- 时钟回拨超过 `clock_rollback_tolerance_ms` 时返回 `E_CLOCK_ROLLBACK`，令牌余额、配额账目、事件水位均不改变。

### 幂等与序号

- 同一幂等键、同一规范化载荷重复提交：原样返回首次裁决结果，不重复扣令牌或占用空间。
- 同一幂等键、不同载荷：`E_IDEMPOTENCY_CONFLICT`。
- 新幂等键但事件序号不大于已裁决水位：`E_EVENT_SEQ_CONFLICT`。
- 所有新裁决，包括限流拒绝和硬配额拒绝，都记录幂等结果。

## 原子性、并发与恢复

- 写入使用 OS 文件锁与 SQLite `BEGIN IMMEDIATE` 双重串行化。
- 配额三层用量、对象归属、令牌、软告警、事件、幂等结果和水位在**同一事务**提交。
- 任意阶段失败或进程被强制终止，SQLite 只保留完整提交事务，不会暴露半提交账目。
- 读取方使用单一 SQLite 快照，不会看到跨查询混合状态。
- `resume` 清理由显式崩溃探针留下的孤儿意图记录，执行 `integrity_check`、WAL checkpoint，并调用全量重放校验。
- `verify` 仅依据配置与已持久化事件从头重算，逐字段比较用量、对象、令牌、软告警、幂等记录、事件元数据和水位。

## 样本与演示

固定样本位于 `samples/`：

- `quotas.json`：3 个租户、10 个层级
- `events.json`：14 个严格递增事件
- `limiter.json`：确定性令牌桶配置
- `clock_manifest.json`：固定随机种子、事件逻辑时钟、重放、崩溃与回拨清单
- `replay_event.json`：幂等重放样本

运行双演示：

```bash
python scripts/demo.py --output-dir artifacts/demo
```

输出：

- `artifacts/demo/run-1.output.json`
- `artifacts/demo/run-2.output.json`
- `artifacts/demo/comparison.json`

验收记录另保存了脚本连续两次执行的标准输出：`artifacts/acceptance-demo-stdout-a.txt` 与 `artifacts/acceptance-demo-stdout-b.txt`。

当前两次输出均为 11,705 字节，SHA-256 均为：

```text
cd097111129edb907b428d8d22c7179b0f5596c514a8fe07c6d84f3043557d25
```

## 测试

```bash
python -m unittest discover -s tests -t . -v
```

当前结果：`21 tests, OK`。覆盖：

- 非法层级、配额、限流配置、乱序事件与批量预校验
- 软限进入/退出、重复不重复告警、硬限拒绝、删除归还
- 令牌拒绝、逻辑等待、重试放行、时钟回拨保护
- 精确幂等重放、幂等冲突、序号冲突
- 故障注入后令牌与配额联合回滚
- 强制终止与孤儿意图恢复
- 篡改后 `verify` 检出且不自动修复
- 4 进程并发不同前缀写入与同幂等键竞争
- 4 进程同序号不同幂等键竞争只有一个赢家
- 全部 CLI 子命令

## 错误码

| 错误码 | 含义 |
|---|---|
| `E_BAD_HIERARCHY` | 层级结构非法、重复、路径未声明 |
| `E_BAD_QUOTA` | 容量/对象上限/百分比非法 |
| `E_BAD_EVENT` | 事件字段或事件流顺序非法 |
| `E_BAD_LIMITER` | 限流参数非法 |
| `E_CONFIG_CONFLICT` | 已有账本加载了不同配置 |
| `E_CLOCK_ROLLBACK` | 逻辑时钟回拨超过容差 |
| `E_IDEMPOTENCY_CONFLICT` | 幂等键复用于不同载荷 |
| `E_EVENT_SEQ_CONFLICT` | 事件序号冲突 |
| `E_OBJECT_NOT_FOUND` | 删除目标不存在（裁决拒绝） |
| `E_RATE_LIMITED` | 令牌不足（裁决拒绝） |
| `E_HARD_QUOTA` | 硬配额不足（裁决拒绝） |
| `E_INJECTED_FAILURE` | 测试故障注入已回滚 |
| `E_RECOVERY_MISMATCH` | 恢复校验不一致 |
| `E_LOCK_TIMEOUT` | 文件锁等待超时 |
| `E_DATABASE` | SQLite 事务失败并回滚 |

