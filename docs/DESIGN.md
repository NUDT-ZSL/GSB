# 设计说明

## 目录与状态

- `generate_samples.py`：固定种子入口，生成两代日志、转发表、补丁、注入时钟和回环服务剧本。
- `proxy_diag/samplegen.py`：确定性样本实现；NumPy 只参与稳定校验和生成。
- `proxy_diag/validation.py`：v1/v2 格式与业务校验，错误均带 `E_*` 码。
- `proxy_diag/merge.py`：跨版本候选建模、裁决和统一视图。
- `proxy_diag/store.py`：SQLite schema、事务、版本化快照和回放认领。
- `proxy_diag/tableflow.py`：转发表补丁、反向依赖闭包和幂等应用。
- `proxy_diag/engine.py`：HTTP 回放、会话锁、崩溃认领恢复。
- `proxy_diag/server.py`：Flask 回环判定服务与调用计数。
- `proxy_diag/verifier.py`：隔离沙箱中的 38 项端到端验证。

运行期布局：

```text
samples/    # 固定种子输入
state/      # SQLite、WAL 与会话文件锁
reports/    # unified_view.json、status.json、diagnostic_report.json
```

## 合并裁决

同一会话存在两个候选时，按以下稳定键取最新：

```text
(declared_logical_clock_ms, generation, source_priority)
source_priority: v2=2, v1=1
```

裁决审计保留每个候选的来源、代数、逻辑时钟、原始时间表示和优先级。v1 的 `record_second` 通过注入时钟换算：

```text
logical_ms  = v1_origin_logical_ms + round(record_second * numerator / denominator)
absolute_ms = base_absolute_ms     + round(record_second * numerator / denominator)
```

每条 v1 记录均记录标度、分子/分母、舍入方向和换算结果。

## 快照与并发

- 清单指纹包含 `manifest_version`、`session_generation_floor`、分片 SHA-256 和转发表摘要集合。
- 旧分片哈希变化、代数下限变化或旧表摘要变化判定为 `full_replace`；仅追加新分片/新摘要判定为 `incremental_append`；同版本同摘要为 `no_operation`。
- 完整替换在一个 `BEGIN IMMEDIATE` 事务中写 staging、版本化 `snapshot_session` 并切换 `snapshot_pointer`。SIGKILL 发生在切换前时事务回滚，旧指针保持有效。
- 回放认领持久化到 `forwarding_attempt`，自然唯一键为 `(connection_id, snapshot_version, generation, table_version)`，另有 `dedupe_hash UNIQUE`。
- 同一会话的跨进程互斥由 `state/session-locks/<hash>.lock` 的 `flock` 保证。第二个显式回放进程等待锁后看到已提交结论，不再发起 HTTP 调用。
- 补丁由全局 `state/workbench.lock` 串行化，并在 SQLite 事务中写表版本、闭包、补丁幂等记录和待重估尝试。

## 恢复

`resume` 对每个 `claimed` 尝试尝试非阻塞会话锁：

- 获得锁说明原进程已死亡，重置为 `pending` 后回放；
- 锁忙说明仍有活动 worker，跳过；
- 已提交结论永不重放或重复计数。

业务状态推进只使用样本/时钟文件中的逻辑毫秒或时钟水位 `42000`，不调用系统墙钟。
