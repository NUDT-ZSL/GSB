# tailsample

`tailsample` 是一个 Windows 本地可运行的分布式追踪尾部采样组件。它只使用固定 JSON 样本、注入逻辑时钟和本地 SQLite，不依赖追踪后端、网络服务、数据库服务或容器。

核心保证：

- 跨度先完整校验再原子写入；非法批次不改变等待池、决策或配额账目。
- 窗口关闭只依赖注入的逻辑时钟，关闭顺序按 `(deadline_ms, trace_id)` 稳定排序。
- 决策在同一 SQLite 事务内提交策略版本、归因证据、链路摘要和配额扣减。
- 决策完成后的跨度只收敛链路摘要，不翻转结果、不再次扣减配额。
- 进程在事务中途退出时由 SQLite 回滚未提交部分；`resume` 只处理未完成事件。
- 写操作使用跨平台文件锁和 `BEGIN IMMEDIATE`，多进程重复采样保持幂等。
- 所有 JSON 输出使用排序键，重复运行逐字节一致。

## Windows 安装

在项目根目录执行：

```bat
py -3.11 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements-dev.txt
pip install -e .
```

也可以直接使用 `python -m tailsample`，无需安装 console script。要求 Python 3.11+、NumPy 最新稳定版和 pytest。

本工作区实际验证环境为 Python 3.11.15、NumPy 2.4.6、pytest 9.2.0、SQLite 3.53.1。

## 快速演示

```bat
demo.bat
```

或：

```bat
.venv\Scripts\python scripts\demo.py
```

脚本会重置 `artifacts/demo.db`，连续执行两次完整演示，分别写入 `artifacts/demo_run1.jsonl` 和 `artifacts/demo_run2.jsonl`，并比较两个文件的 SHA-256。

恢复演示：

```bat
.venv\Scripts\python scripts\recovery_demo.py
```

该脚本在配额扣减后强制退出进程，再执行 `resume`，并与无故障基线逐追踪比较。

## 样本场景

`samples/` 包含：

- `topology.json`：4 个服务的拓扑。
- `policies.json`：`policy-v1` 与在逻辑时钟 2500 ms 生效的 `policy-v2`。
- `events.json`：15 个乱序跨度事件，覆盖 7 条追踪。
- `late_spans.json`：决策提交后到达的跨度。
- `invalid_span.json`、`unknown_service.json`：错误隔离样本。

默认窗口为 900 ms，每个逻辑秒最多保留 2 条追踪。

| 追踪 | 关闭原因 | 结果 | 原因 |
| --- | --- | --- | --- |
| `trace-checkout-001` | 根跨度结束 | KEEP | 错误率与属性条件命中 |
| `trace-order-002` | 根跨度结束 | KEEP | 高价值属性命中 |
| `trace-slow-003` | 根跨度结束 | DROP | 配额耗尽 |
| `trace-batch-004` | 根跨度结束 | KEEP | 状态码错误命中 |
| `trace-policy-006` | 根跨度结束 | KEEP | 关闭时绑定 `policy-v2` |
| `trace-orphan-005` | 窗口到期 | KEEP | 慢延迟桶 |
| `trace-fast-000` | 根跨度结束 | DROP | 哈希未命中 5% 采样率 |

`trace-checkout-001` 和 `trace-slow-003` 随后各收到一个迟到跨度。两条追踪的链路摘要被重算，但 KEEP/DROP 结果与配额扣减保持不变。

## 命令行

所有命令默认数据库为 `artifacts/tailsample.db`，可用 `--db` 覆盖。

```bat
python -m tailsample load ^
  --db artifacts/demo.db ^
  --topology samples/topology.json ^
  --policies samples/policies.json ^
  --events samples/events.json ^
  --window-ms 900 ^
  --quota-per-second 2 ^
  --clock-tolerance-ms 10 ^
  --reset

python -m tailsample sample --db artifacts/demo.db --advance-to 3500

python -m tailsample sample ^
  --db artifacts/demo.db ^
  --advance-to 4000 ^
  --late-spans samples/late_spans.json

python -m tailsample query --db artifacts/demo.db --trace-id trace-checkout-001
python -m tailsample policy list --db artifacts/demo.db
python -m tailsample status --db artifacts/demo.db
python -m tailsample verify --db artifacts/demo.db
python -m tailsample resume --db artifacts/demo.db
```

`query` 输出稳定排序的跨度链路、决策、原因、逐条证据、策略版本、链路摘要和迟到收敛信息。`status` 输出等待池及高水位、当前逻辑秒配额余量、迟到计数和恢复计数。`verify` 校验 SQLite 完整性、决策链摘要、决策指纹、策略绑定和配额账目。

`load` 只做完整校验并原子暂存事件；`sample` 才按逻辑时钟把事件推进等待池、关闭窗口并提交决策。因此刚执行 `load` 后等待池为空、`staged_events.pending` 等于事件数，这是预期状态。

## 决策模型

策略按关闭时的逻辑毫秒选择：`activated_at_ms <= decided_at_ms` 中激活时间最新、修订号最高的版本。进行中的追踪不会提前绑定策略。

决策证据包括：

- 追踪错误率：`is_error == true` 或 `status_code >= 500` 的跨度占比。
- NumPy 线性插值延迟分位数，以及命中的延迟桶。
- 每条自定义属性条件、观测值、运算符、阈值和采样率。
- 稳定 SHA-256 哈希选择结果。
- 逻辑秒、配额容量、扣减前后用量。
- 关闭原因、策略版本、修订号和摘要。

有效目标采样率取所有命中信号中的最大采样率。哈希未命中时原因为 `SAMPLING_RATE_NOT_SELECTED`；命中但逻辑秒配额已满时原因为 `QUOTA_EXHAUSTED`。两者在决策和证据中明确区分。

## 事务与恢复

每次事件处理依次执行：

1. `BEGIN IMMEDIATE`。
2. 关闭所有 `deadline_ms <= arrival_ms` 的等待追踪。
3. 写入事件跨度并更新等待池。
4. 根结束时提交决策；必要时扣减配额。
5. 记录关闭序号、链路摘要、生命周期事件。
6. 将事件标记为已处理并提交。

任一步失败都会回滚整个事务。`TAILSAMPLE_FAULT_POINT` 支持 `during_window_close`、`after_quota` 和 `after_decision_insert`，会在对应步骤后、事务提交前调用 `os._exit(97)`，用于验证 SQLite 日志恢复。`resume` 读取持久化的逻辑时钟、命令目标和未处理事件，从首个未完成事件继续，并记录已提交但被跳过的决策数。

文件锁位于数据库旁的 `<db>.lock`。Windows 使用 `msvcrt.locking`，POSIX 使用 `fcntl.flock`；SQLite 同时启用 WAL、`synchronous=FULL`、外键和 30 秒 busy timeout。

## 业务错误码

| 错误码 | 场景 |
| --- | --- |
| `E_BAD_SPAN` | 跨度结构、布尔位、数值属性或重复跨度非法 |
| `E_BAD_TOPOLOGY` | 服务重复、多根、自父或父链成环 |
| `E_UNKNOWN_SERVICE` | 引用未声明服务 |
| `E_MISSING_PARENT` | 父跨度缺失 |
| `E_TIME_INVERSION` | 结束逻辑时钟早于开始 |
| `E_ATTRIBUTE_TYPE_CONFLICT` | 属性键类型冲突或重复键冲突 |
| `E_POLICY_ROLLBACK` | 策略版本或激活时间回退 |
| `E_QUOTA_MISMATCH` | 配额账目与保留决策不一致 |
| `E_CLOCK_ROLLBACK` | 逻辑时钟回拨超过容差 |
| `E_LATE_SPAN_REJECTED` | 关闭时策略拒绝迟到跨度 |
| `E_DECISION_CONFLICT` | 同一追踪出现冲突决策 |
| `E_CONFIG_CONFLICT` | 已写入数据后修改固化的引擎配置 |
| `E_EVENT_CONFLICT` | 同一事件 ID 内容不一致 |
| `E_VERIFY_FAILED` | 一般一致性校验失败 |
| `E_NOT_FOUND` | 查询对象不存在 |
| `E_BAD_INPUT` | 文件或命令参数非法 |

异常追踪会进入 `quarantined_traces`，不影响其他追踪关闭或已提交决策。`query` 会返回隔离错误码与诊断字段。

## 测试

```bat
.venv\Scripts\python -m pytest
```

测试覆盖拓扑与跨度校验、非法批次零状态变更、窗口与配额裁决、策略版本绑定、迟到跨度收敛顺序、重复迟到幂等、强制终止恢复、并发采样、稳定输出和两次演示逐字节一致。

实际验证结果见 `VALIDATION.md`。完整演示日志保留在 `artifacts/demo_run1.jsonl` 和 `artifacts/demo_run2.jsonl`。

