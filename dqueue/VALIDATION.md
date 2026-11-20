# 验证报告

运行环境（本地，无容器、无外部服务、无网络依赖）：

- Python 3.11.15（通过 `uv python install 3.11` 安装的独立解释器）
- NumPy 2.4.6；标准库 `sqlite3`、`json`、`heapq`、`multiprocessing`、`fcntl` 可用
- 复现命令：

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -e .
.venv/bin/python -m unittest discover -s tests      # 38 个用例
.venv/bin/python demo.py                            # 端到端演示
.venv/bin/python demo_adaptive.py                   # 自适应切换演示
```

## 1. 全量单元/集成测试

`python -m unittest discover -s tests` 实际结果（连续 5 轮全部 `OK`）：

```
Ran 38 tests in 5.0s ~ 7.1s
OK
```

覆盖矩阵：

| 测试模块 | 用例数 | 覆盖业务要求 |
| --- | --- | --- |
| `test_scheduler_equiv.py` | 7 | 堆/轮逐位一致、跨视界、500 条随机流、切换规则与迟滞、开销确定性 |
| `test_manifest_backoff.py` | 9 | 清单/策略全部校验分支、退避确定性与边界、逻辑时钟回拨 |
| `test_queue_flow.py` | 11 | 入队、成功/失败、退避重试、死信、幂等、跨消费者、租约重投、回拨、版本回退、verify、双模式序列一致、非法输入不落库 |
| `test_adaptive_switch.py` | 2 | heap→wheel 切换记录与观测值、切换零丢失零重复 |
| `test_recovery.py` | 4 | 入队/触发/确认三处 SIGKILL 崩溃点的恢复语义 |
| `test_concurrency.py` | 2 | 6 进程并发扫描 exactly-once、5 进程并发确认重试不超增 |

## 2. 两种调度模式到期序列逐位一致

`demo.py` 第 1 节对同一样本流与同一逻辑时钟步进，分别驱动堆与时间轮：

```
"byte-identical" => true
heap  序列: t-001,t-002,t-003,t-004,t-005,t-006,t-007,t-008,t-009,t-010,t-011,t-012
wheel 序列: t-001,t-002,t-003,t-004,t-005,t-006,t-007,t-008,t-009,t-010,t-011,t-012
```

`test_large_random_stream_matches` 另以固定种子随机 500 条任务、40 个时钟步进验证一致。

## 3. 自适应切换

`demo_adaptive.py`（128 个任务、跨度 19843ms、阈值 64、迟滞 0.8）实际输出：

```
"no_loss_no_duplicate": true
"triggered_total": 128
"triggered_unique": 128
switch 1: heap -> wheel, now=500,  n=128, heap=897.4 wheel=326.4,
          reason=wheel_cost_below_heap_with_margin
switch 2: wheel -> heap, now=17000, n=57, heap=333.9 wheel=144.5,
          reason=pending_below_threshold(57<64)
```

切换前后 `n`、跨度、两项开销、原因、版本号均落库于 `mode_switch_log`，
`status` 子命令可查。切换不搬运任务（任务只存 SQLite），在途到期任务零丢失零重复。

## 4. 退避重试与死信

- 确定性：`jittered` 策略 4 档退避 `[230, 460, 919, 1838]`，重复计算完全相同
  （`"recomputed identical" => true`）。
- 固定策略为纯指数：500 → 1000 → 2000 → 封顶 4000。
- 端到端演示中 `t-002`（`max_retries=1`）两次失败后进入死信：

```
{'task_id': 't-002', 'stage': 'DEAD', 'attempts': 2,
 'dead_reason': 'RETRY_EXHAUSTED', 'last_error_code': 'E_DEMO_FAILURE'}
阶段统计: {'DEAD': 1, 'SUCCEEDED': 11}
```

死信任务在后续 `scan` 中不再被触发。

## 5. 强制终止恢复

`test_recovery.py` 用子进程 `os._exit(99)` 模拟 SIGKILL，实际结果全部 `ok`：

- `test_crash_during_enqueue_writes_nothing`：入队事务中途被杀，任务表与快照表均为 0 行，
  恢复后可重新完整入队并 `verify` 通过。
- `test_crash_after_pull_insert_before_stage_update_rolls_back`：触发事务回滚，无 pull 残留、
  任务仍为 `SCHEDULED`，重新扫描恰好触发一次。
- `test_crash_after_stage_update_keeps_durable_trigger`：提交后被杀，触发保持 `RUNNING`，
  不重复触发，已有租约可被确认且重复确认幂等。
- `test_crash_during_ack_does_not_double_apply`：确认事务提交前被杀，重试次数不增加，
  恢复后一次真实确认即达 `SUCCEEDED`。

## 6. 多进程并发幂等

`test_concurrency.py`（spawn 多进程 + `fcntl` 文件锁 + SQLite 事务）：

- 6 个进程同时 `scan(1000)`：40 个任务触发总数 40、去重后 40，每个任务恰一条 pull。
- 5 个进程同时对同一 `pull_id` 做失败确认：仅 1 次真正落库，其余返回 `idempotent=true`，
  `attempts` 严格为 1，下次触发时钟落在获胜时钟 +100ms 的确定区间内。

## 7. 异常链路（不同错误码实测退出码）

```
E_PAYLOAD_DIGEST_MISMATCH -> exit 28（且数据库文件未被创建）
E_FOREIGN_CONSUMER        -> exit 32
E_PULL_NOT_FOUND          -> exit 31
E_CLOCK_ROLLBACK          -> exit 29（诊断含 now_ms/high_water_ms/rollback_ms/tolerance_ms）
E_MODE_VERSION_ROLLBACK   -> exit 30（诊断含 requested/current 版本号）
```

每个错误均携带 `error_code`、`message` 与 `diag` 诊断字段；单次异常不影响其他任务调度。

## 8. 确定性与墙钟隔离

- 代码扫描 `rg "time\\.time|datetime\\.now|time\\.monotonic|perf_counter" dqueue/` 无业务命中
  （仅 `clock.py` 文档字符串中的说明文字）。
- 所有演示时刻来自 `samples/clock_steps.json`，重复运行产生相同数据库状态与相同 JSON 输出。
- 产物全部位于工作区 `run/` 下：`demo/demo.db*`、`demo/logs/demo-run.json`、
  `adaptive/adaptive-report.json`。
