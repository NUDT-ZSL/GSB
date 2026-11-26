# 实际验证记录

验证时间：2026-11-27（Asia/Shanghai）

## 环境

- Python `3.10.12`
- fastapi `0.115.6`
- uvicorn `0.32.1`
- pytest `8.2.0`
- SQLite 标准库 `sqlite3`
- 未使用 Docker，未访问网络目标

## 自动化测试

命令：

```bash
python3 -m pytest -v
```

实际结果：

```text
collected 11 items

tests/test_api.py .
tests/test_engine_diagnostics.py ...
tests/test_parsing_and_policy.py .....
tests/test_policy_store.py .
tests/test_recovery.py .

======================== 11 passed, 1 warning in 1.32s ========================
```

唯一警告来自 Starlette/TestClient 对 anyio 别名的弃用提示，不影响功能。

完整日志保存在 `artifacts/pytest-verification.log`。

## 确定性与 SIGKILL 恢复

命令：

```bash
python3 -m cold_diag.cli generate-samples
python3 -m cold_diag.cli run --db artifacts/final3-clean-a.sqlite \
  --output artifacts/final3_clean_a.json
python3 -m cold_diag.cli run --db artifacts/final3-clean-b.sqlite \
  --output artifacts/final3_clean_b.json
python3 -m cold_diag.cli crash-demo --step round:warm-r1 \
  --db artifacts/final3-crash.sqlite \
  --output artifacts/final3_crash.json
```

实际状态哈希：

```text
clean_a_state_sha256=4927c68609b5889efa6fdea251b8846b19f77a3cd0157583bd4ef7bac40d7b04
clean_b_state_sha256=4927c68609b5889efa6fdea251b8846b19f77a3cd0157583bd4ef7bac40d7b04
recovered_state_sha256=4927c68609b5889efa6fdea251b8846b19f77a3cd0157583bd4ef7bac40d7b04
clean_runs_byte_identical=true
recovery_matches_clean=true
crash_subprocess_returncode=-9
half_committed_steps=round:warm-r1
final_cold_start_watermark={"committed_budget_charge_total_ms": 500, "max_scheduled_failure_at_ms": 165, "max_scheduled_ready_at_ms": 290, "peak_warm_concurrency": 2, "started_charge_count": 6}
```

恢复审计识别到 `round:warm-r1` 已写入 prepared 意图，但没有提交标记，且未提交事务中的预算扣款数为 `0`。重放后 SQLite 中 `warm-r1` 恰有两条扣款：

```text
("alpha-1", 100, 1)
("beta-1", 80, 1)
```

这证明未提交扣款随 SQLite 事务回滚，重放使用相同幂等键且不重复扣减。

完整命令输出保存在 `artifacts/acceptance-verification.log`。

## 分发与状态演进

恢复后的实际分发时间线：

```text
1 -> alpha-1@100
2 -> alpha-1@100
3 -> beta-1@80
4 -> gamma-1@130
5 -> alpha-2@210
6 -> beta-1@120
7 -> gamma-1@130
8 -> delta-3@290
```

关键并发结论：

- `seq=1/2` 都在逻辑时钟 `100` 分发到 `alpha-1`，验证实例并发上限 `2`。
- `seq=5` 到达时 `alpha-1` 的两个并发槽仍被长调用占用，因此创建并启动 `alpha-2`。
- 所有分发时钟均不早于对应实例的 `ready_at_ms`，不会分发到 `warming` 或 `recycling`。
- `beta-1` 经历 `ready -> busy -> ready -> recycling -> recycled`，验证预热窗口回收。

候选结局：

```text
alpha-1   started
alpha-2   started
beta-1    started
gamma-1   started
delta-3   started
flaky-1   warmup_failed / deterministic_warmup_failure
flaky-2   not_started_in_budget / global_cold_start_limit
delta-1   abandoned / global_cold_start_limit_tail
delta-2   abandoned / insufficient_logical_budget
```

## 定点账本与精度敏感

样本声明 `unit_ms=1000`、`scale=100`，即一个定点整数表示 `10ms`。每次换算都记录：

- 标度与源单位；
- 舍入方向；
- 精确 scaled rational；
- 被舍弃余量；
- 本次误差界与累计误差上界。

精度敏感对：

```text
seq=1 wait=100ms
seq=2 wait=96ms
difference=4ms
tolerance=10ms
sensitive=true
resolution=mark_sensitive_keep_priority_then_request_seq
```

## 策略快照

`tests/test_policy_store.py` 实际覆盖：

- v2 包分片 SHA-256 与键集合摘要校验；
- 全量替换到 version `3`；
- 基于 version `3` 的增量补丁到 version `4`；
- `pool:gamma` 变更后闭包包含 `gamma` 与反向依赖 `gateway`；
- 旧 version `2` 包再次提交时抛出 `PolicyVersionRollbackError`；
- SQLite 中保留 version `2/3/4` 快照行，活动指针指向 `4`，证明不是逐条覆盖当前记录。

## HTTP/Uvicorn 冒烟

实际启动：

```bash
python3 -m uvicorn cold_diag.api:app --host 127.0.0.1 --port 8811
```

冒烟结果：

```text
GET  /health    -> {"status":"ok","service":"cold-diag"}
POST /diagnose  -> processed_request_seq=8
                   canonical_state_sha256 length=64
```

## 需求到实现映射

1. 输入校验：`cold_diag/parser.py`、`tests/test_parsing_and_policy.py`。
2. 两代策略合并：`cold_diag/policy.py` 的 `merge_policy_records`。
3. 注入时钟状态机：`cold_diag/engine.py` 的 `DiagnosticEngine.advance_to`。
4. 预算与全局冷启动槽：`DiagnosticEngine.schedule_round`。
5. 定点账本：`cold_diag/ledger.py`。
6. 确定性分发：`_eligible_instance`、`_allocate`、`_dispatch_queued`。
7. 策略替换/增量/闭包：`cold_diag/policy_store.py`。
8. SIGKILL 恢复：`cold_diag/store.py`、`tests/test_recovery.py`。
