# 技术设计

## 1. 分层

- `SceneLoader`：把 JSON 转为不可变领域模型。语义校验在写入任何会话状态之前完成，所有问题一次性返回 `E_SCENE`。
- `ProbeRunner`：唯一探测入口。`ThreadPoolExecutor(max_workers=global_concurrency)` 提供全局上限，每服务 `Semaphore(service_quota)` 提供配额；公平轮转按服务交错提交实例任务。
- `HealthStateMachine`：连续成功/失败计数与阈值迁移，不读取墙钟做状态判断。
- `SessionRepository`：SQLite 会话库。所有状态、探测尝试、业务事件、切换事务和快照均持久化。
- `FailoverEngine`：编排探测、状态迁移、切换、一致性复探、快照与重放。
- `Tkinter GUI` / CLI：只负责编辑与操作入口，复用同一引擎。

## 2. 场景校验

校验器使用“解析后的对象 + 源文本行索引”：

- 结构错误来自 `json.JSONDecodeError.lineno`。
- 语义错误由字段路径与附近 ID 定位行号；返回 `line / field / message`。
- 未知字段被拒绝，避免字段拼写错误被静默忽略。
- 出现任一问题即整体拒绝，不初始化或修改会话库。

场景指纹为规范化 JSON 的 SHA-256；`scene_version`、探测预算、服务/实例参数和场景故障项都参与指纹计算。

## 3. 探测与预算

每个实例任务以实例为粒度获取配额，重试循环在同一任务内执行，因此重试不能绕过配额。尝试记录包括：

```text
run_id, round_no, service_id, instance_id, attempt_no,
conclusion, detail, started_mono_ms, ended_mono_ms,
created_epoch_ms, fault_kind
```

状态判定使用最终尝试结论：任一尝试成功即停止重试；全部失败时保留每次尝试，并记录 `E_PROBE` 业务事件。

四类结论的来源：

- `success`：HTTP 2xx。
- `error`：非 2xx、空响应、畸形 HTTP 或本地注入 503。
- `timeout`：socket/OS 超时或本地注入超时。
- `unreachable`：连接拒绝、网络不可达、连接重置或本地注入不可达。

## 4. 健康状态机

```text
unknown --success_threshold--> healthy
unknown --failure_threshold--> unhealthy
healthy --failure_threshold--> unhealthy
unhealthy --success_threshold--> healthy
known/no-evidence ------------> unknown
```

每次迁移写入 `state_transitions`，并同时更新 `instances.health_state` 与连续计数。缺失探测记录时不会保留“健康”推断，而是回到 `unknown` 并要求重跑。

## 5. 切换事务

触发条件：

- 主实例不是 `healthy`，或角色拓扑异常；
- 非隔离健康副本数低于 `min_healthy_replicas`。

目标排序：

```text
(instance_id, port, version)
```

仅健康、未隔离的备用实例可成为目标。一个切换的状态推进如下：

1. `BEGIN IMMEDIATE` 插入 `switch_transactions(service_id PRIMARY KEY)`；唯一键冲突立即返回 `E_BUSY`。
2. 在同一角色提交事务中校验源仍为主、目标仍为备，原子更新两侧角色与版本，隔离源实例，写冷却起点。
3. 仍持有一个进行中事务期间，重新探测该服务全部实例。
4. 校验唯一主实例、健康副本数与源隔离标记；写 `SWITCHED` 或 `E_ISOLATE`。
5. 删除事务行。若进程在此前崩溃，事务行保留，下次恢复把该服务置为 `unknown` 并要求重跑。

冷却起点为墙钟 epoch 毫秒，原因是跨进程单调时钟不可比较；单次探测耗时仍使用单调时钟。

## 6. SQLite 表

- `instances`：角色、端口、版本、隔离、健康状态、连续计数、探测参数、重跑标记。
- `policies` / `service_runtime`：策略、冷却起点、最后目标、切换中标记。
- `probe_attempts`：不可变探测尝试。
- `state_transitions`：状态迁移审计。
- `business_events`：`E_PROBE`、`E_ISOLATE` 等业务事件。
- `switch_decisions`：所有切换决策，包括 `E_COOLDOWN`、`E_BUSY`、`E_MANUAL_PENDING`。
- `switch_transactions`：服务级切换互斥与崩溃检测。
- `pending_switches`：人工确认队列。
- `snapshots`：完整 JSON 快照与水位线。

数据库使用 WAL、`synchronous=FULL`、`busy_timeout=10000`。状态迁移采用“实例更新 + 迁移插入”同一事务。

## 7. 快照与恢复

快照包含：

- `scene_version`、场景指纹、完整规范化场景配置；
- 周期前 `before`、周期后 `after` 完整运行态；
- 策略版本、实例角色/版本、健康状态、隔离标记、冷却起点；
- 已完成切换与待确认请求；
- 可重放轨迹 `trace`；
- 各类事件的 ID 水位线。

恢复策略：

1. 打开最新快照并整体删除/重建实例、策略、服务和待确认请求，不做字段级增量合并。
2. 当前场景版本与快照不同：报告不匹配，相关服务全部 `unknown`，必须选择匹配场景后才能运行。
3. 存在 `switch_transactions`、`switching=1` 或快照水位线之后的新事件：相关服务置 `unknown`，清除残留事务，要求重跑。
4. 后续事件仍保留用于审计，但不会被误认为一致的健康状态。

## 8. 重放

重放在临时 SQLite 中进行，不改变在线会话：

1. 比较当前场景与快照 `scene_config`，输出字段级差异，例如 `services.0.policy.version`。
2. 用快照 `before` 状态重建临时运行态。
3. 使用原周期 `started_epoch_ms`、原运行时故障映射执行同一引擎路径。
4. 比较探测尝试结论、状态迁移、切换决策、业务事件和业务码。
5. 任一差异返回 `E_REPLAY`；完全一致返回 `OK_REPLAY`。

时间戳、UUID、单调时钟值不参与比较，避免无可重放性的噪声。

## 9. 异常注入与日志

- `scene-corruption`：加载器在解析后抛出带行号字段的 `E_SCENE`。
- `unreachable`：按实例加入本轮运行时故障，返回 `E_PROBE` 并按策略尝试切换。
- `snapshot-write-failure`：所有状态照常落库，快照写入口抛 `E_SNAPSHOT`。
- `JsonlLogger` 对每条记录执行 `flush + fsync`，CLI 关闭 stdout 回显以保证业务 JSON 可解析。

## 10. 已知边界

- 当前环境未安装 Tkinter，GUI 未进行像素级人工操作验证；无头环境会安全退出并提示安装。
- 场景故障项在文件版本内应保持稳定；若修改参数但不提升 `scene_version`，重放会通过指纹/字段差异拒绝。
- `--port-map` 会改变有效场景指纹，所有命令与 mock 必须使用相同映射。
