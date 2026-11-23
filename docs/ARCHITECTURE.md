# 架构与一致性设计

## 1. 组件

| 组件 | 职责 |
| --- | --- |
| `models.py` / `loader.py` | 解析 JSON，校验唯一性、整数范围、有限数值、逻辑时钟序列和本地回环目标 |
| `conversion.py` | 服务级观测到目标副本数换算，输出完整证据 |
| `packing.py` | 拓扑、节点并发、单卡显存三重筛选，原子选择落点 |
| `backoff.py` | 仅依赖注入逻辑时钟的指数退避与确定性抖动 |
| `engine.py` | 两阶段轮次协议：预占意图、并发执行、持久化裁决 |
| `storage.py` | SQLite WAL、文件锁、快照表和 `current_snapshot` 指针 CAS |
| `state.py` | 副本状态机、服务统计、GPU/并发账本守恒校验 |
| `launch_client.py` | 回环 HTTP 客户端，显式绕过环境代理 |
| `stub.py` | 幂等 `/launch`、`/operations/{id}`、`/health` 与 `/stats` |
| `workbench.py` | 只读诊断 API、运行/恢复 API、Flask 页面 |

## 2. 状态模型

核心状态保存在单份 JSON 快照中，包括：

- 配置版本、规格摘要、逻辑时钟水位、轮次水位、状态修订号。
- 每个服务的当前目标、各阶段副本数、连续拉起/健康失败计数。
- 每个节点的每卡剩余显存和已用实例数。
- 每个副本的阶段、落点、GPU 索引、退避截止、失败分类和历史结果。
- `pending_rounds`：已预占但尚未完成裁决的操作意图。
- `placement_decisions`：装箱证据与幂等结论。
- 最近 50 轮核心轮次历史。

副本阶段：

```text
PENDING ──拉起预占──> COLD_STARTING ──成功──> READY
   ^                         │
   └────拉起失败 + 退避──────┘

READY ──目标下降/健康阈值──> DRAINING ──排空截止到达──> RECYCLED
PENDING ──目标下降──> RECYCLED
```

`COLD_STARTING`、`READY`、`DRAINING` 占用显存和节点实例额度；`DRAINING` 不计入可用副本；`PENDING` 与 `RECYCLED` 不占额度。

## 3. 两阶段轮次协议

每轮分为准备、执行、裁决三段：

### 准备事务

1. 获取数据库文件锁。
2. 读取当前快照并校验账本。
3. 检查配置版本、逻辑时钟回拨、服务过滤和幂等键。
4. 完成排空回收、目标换算、目标副本补齐/缩减。
5. 按服务优先级和稳定次序为候选副本装箱。
6. 对已选副本扣减本轮逻辑预算，更新 GPU/并发账本，写入 `COLD_STARTING` 和 `RESERVED` 操作。
7. 在一个 SQLite 事务内插入新快照、切换 `current_snapshot`、写 `round.prepared` 事件。
8. 释放文件锁，进入网络执行阶段。

关键点：预算和资源在发出 HTTP 之前已经持久化。因此即使进程在请求中途死亡，恢复也不会再次占用预算或显存。

### 并发执行

- 操作按服务分组。
- 每个服务组使用独立线程池，线程数不超过 `per_service_launch_limit`。
- 主线程等待所有已提交 future 完成，不使用结果超时替代真实等待。
- HTTP socket 超时只作为目标不可用的传输错误边界；已启动请求必须得到响应或明确传输失败。

### 裁决事务

1. 重新获取文件锁并读取最新快照。
2. 验证该轮仍以 `prepared` 状态存在。
3. 按稳定操作 ID 顺序应用完整结果。
4. 拉起成功进入 `READY`；失败释放落点、增加拉起失败计数并计算退避。
5. 健康探测失败增加独立计数；达到阈值后进入 `DRAINING`。
6. 计算三类失败结局计数、错误码、退避计划、阶段计数和账本快照。
7. `state_revision + 1`、轮次水位推进、移除对应 pending round。
8. 同一事务内插入最终快照、切换指针并写 `round.completed` 事件。

技术快照 ID 可以在中断恢复中比无中断运行多一次，因为它属于存储层物理提交次数；业务状态摘要（阶段、账本、预算、退避、历史）保持逐字段一致。

## 4. 恢复算法

恢复只读取当前快照的 `pending_rounds`：

```text
for pending_round in ascending round order:
    for operation in stable operation order:
        if result already persisted:
            reuse result
        elif launch:
            GET /operations/{operation_id}
            if 404:
                POST /launch with same operation_id
        elif health:
            GET /health?operation_id=...
    finalize round from persisted reservations
```

恢复约束：

- 不创建新轮次，不增加预占。
- 不按墙钟重新计算退避。
- 不重复选择落点。
- 已被其他控制面实例完成并移出 `pending_rounds` 的轮次直接视为幂等完成。
- 恢复决策写入事务内 `recovery.applied` 事件，并写入 `recovery_audit` 表供工作台展示。

## 5. 并发与事务

- 每个状态写事务使用 `BEGIN IMMEDIATE`。
- 使用数据库旁路 `.lock` 文件和操作系统文件锁串行化同一数据库的状态写入。
- 网络执行阶段释放锁，多个控制面实例可以并行处理不同服务。
- 准备事务已把内存/并发额度写入快照，因此另一个实例会看到并尊重这些预占，不会超卖。
- `expected_snapshot_id` 用于 CAS；若事务开始后发现当前指针已改变，返回 `E_STATE_VERSION_CONFLICT`。
- 每个快照整体写入 `snapshots.state_json`，`current_snapshot` 单行指针在事务内切换。

## 6. 确定性与账本不变量

确定性来源：

- 服务级负载序列按 `logical_time_ms` 排序，取不晚于当前时钟的最后一项。
- 十进制运算固定小数输入表示和聚合顺序。
- 候选顺序固定为优先级、服务名、副本序号。
- 装箱节点按名称升序，GPU 按索引升序。
- 退避抖动使用 SHA-256，不使用 Python 随机数或对象哈希。
- 预算和退避只使用逻辑毫秒值。
- 调度结果不持久化墙钟时间。

提交前不变量：

- 每个节点账本余额等于物理容量减去所有占用副本在该节点的分配。
- `used_instances` 等于占用副本数，不超过节点上限。
- 非占用阶段副本不得持有落点。
- 副本显存需求必须等于当前服务规格。
- 不得出现负余额、超容量或重复副本序号。

任一不变量失败都返回 `E_LEDGER_MISMATCH`，事务回滚且指针不变。

## 7. 错误分类

拉起失败会进入轮次裁决和副本退避，而不是让整个控制面丢失状态：

- 连接拒绝：`E_LAUNCH_CONNECTION_REFUSED`
- 响应超过冷启动逻辑预算：`E_COLD_START_BUDGET_EXCEEDED`
- HTTP/传输超时：`E_LAUNCH_HTTP_ERROR`
- 响应格式非法：`E_LAUNCH_RESPONSE_INVALID`

配置/账本类错误是控制面级业务错误：

- 配置版本回退：`E_CONFIG_VERSION_REGRESSION`
- 账本不守恒：`E_LEDGER_MISMATCH`

## 8. 工作台

`GET /api/status` 输出稳定排序 JSON；`GET /api/events` 输出事件链、恢复审计和快照链；`POST /api/run` 与 `POST /api/recover` 触发对应控制面动作。

页面展示：

- 逻辑时钟、轮次水位、状态修订、当前预算消耗。
- 服务目标、READY/COLD/PENDING/DRAINING 数量和失败计数。
- 每节点每 GPU 的剩余显存与实例占用。
- 副本阶段、落点、退避截止和最后错误码。
- 轮次换算证据、预算裁决、三类未成功结局、装箱淘汰原因。
- 恢复动作列表，明确 `budget_recharged: false` 和重放规则。
