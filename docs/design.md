# 技术设计

## 模块边界

- `models.py`：输入数据类、规范化、业务校验和样本摘要。
- `filters.py`：五个过滤插件及首个淘汰证据。
- `scoring.py`：三个打分插件、精确有理数归一化和六位量化。
- `engine.py`：单 Pod 裁决、普通淘汰、绑定和注入归属。
- `storage.py`：SQLite schema、不可变账本、原子提交、回放和对账。
- `service.py`：文件锁、加载、调度、恢复、状态和报告输出。
- `cli.py`：六个核心子命令、额外 `replay` 诊断命令和稳定 JSON 输出。

## 输入摘要

解析后节点按 name、Pod 按 name、注入按
`(round, phase_order, pod, id)` 排序。配置中的过滤插件顺序规范化为固定链
顺序，权重按插件名排序。四部分规范化数据生成一个 SHA-256 摘要：

```json
{
  "schema": "scheduler-diag/v1",
  "nodes": [],
  "pods": [],
  "config": {},
  "injections": []
}
```

摘要同时写入 manifest、每个归属条目和每版账本。`status`、`resume` 和
`verify` 都从 manifest 恢复同一摘要；请求样本摘要不同即返回
`E_DIGEST_MISMATCH`。

## SQLite 模型

| 表 | 用途 |
| --- | --- |
| `meta` | 输入摘要、轮次水位、注入水位、事务重试计数 |
| `manifest` | 规范化样本和摘要 |
| `nodes` | 总量与当前余量，始终满足 `remaining >= 0` |
| `pods` | 不可变 Pod 请求与优先级 |
| `pod_states` | `pending/filtering/scoring/binding/done` 与最终归属版本 |
| `rounds` | 轮次完成状态 |
| `allocations` | 每 Pod 唯一资源扣减、来源轮次、账本版本和条目摘要 |
| `injection_consumptions` | 连续消费序号、注入 ID、目标与归属版本 |
| `ledger_versions` | 不可变完整账本快照与 SHA-256 |
| `ledger_pointer` | 唯一已提交账本版本 |
| `clock_state` | 全局逻辑毫秒、当前轮次、本轮已用逻辑毫秒 |

触发器拒绝更新或删除 `ledger_versions` 和 `allocations`，也拒绝修改已完成
的 `pod_states`。账本变化只能通过插入新版本并更新单行指针完成。

## 单 Pod 提交不变量

提交事务开始时必须满足：

- Pod 阶段不是 `done`，且没有 allocation。
- 目标节点余量足以覆盖 Pod 请求。
- 输入摘要与 manifest 一致。

提交事务结束时必须同时满足：

- 目标节点余量恰好减少 Pod 请求。
- 恰好插入一条 allocation，记录轮次、Pod、节点、请求、账本版本和条目摘要。
- 恰好插入一个账本版本并推进指针。
- Pod 阶段为 `done`，终态和归属版本指向新账本。
- 如消费注入，消费序号连续且账本条目标记同一 ID。
- 逻辑时钟单调不减。
- 轮次完成状态与全部 Pod 阶段一致。

任何一步异常都会回滚整个事务，因此不存在“扣了资源但没有归属条目”或
“条目已写入但资源未扣减”的可见状态。

## 注入作用域

注入按 `(pod, round)` 查找，且输入校验保证最多一个。过滤阶段注入直接生成
目标 Pod 的失败归属条目；打分阶段注入仍计算正常过滤和打分中间值，然后生成
打分失败归属条目，不执行扣减。归属条目包含目标轮次、Pod、节点、插件、注入
类型和独立错误码。

注入从不修改节点、Pod 清单或其他节点资源。目标 Pod 失败后释放事务，调度器
继续本轮下一个 Pod。

## 逻辑时钟

逻辑时钟不使用 wall clock。每执行一个过滤插件或一个节点上的一个打分插件，
时钟增加 `config.logical_clock.plugin_tick_ms`。时钟状态随 Pod 归属事务一起
持久化；进程强杀时未提交的 tick 回滚。每轮开始时本轮已用时间归零，单轮预算
超出会产生 `RoundTimeBudget` 归属失败，不扣资源。

## 回放与对账

回放执行以下检查：

1. manifest 摘要与请求样本摘要一致。
2. 账本版本从 0 连续到指针版本，没有指针外版本。
3. 每个快照 SHA-256 正确，条目只追加、不修改。
4. 每个条目摘要、输入摘要和注入消费标记正确。
5. 注入消费序号连续，entry 水位累加与消费记录一致。
6. 每个完成 Pod 恰有一个归属条目。

对账继续检查：

- 每个节点 `total - sum(debits) == remaining` 且不为负。
- 每个 allocation 的请求等于 Pod 请求，轮次等于到达轮次。
- 每个 allocation 指向 `bound` 归属，节点和条目摘要一致。
- 每个 `bound` 归属都有 allocation，不存在悬挂关系。
- 前/后资源值与 allocation 请求一致。

## 并发与幂等

每个数据库对应一个 `<db>.lock` 文件，POSIX 使用 `fcntl.flock`，Windows 使用
`msvcrt.locking`。所有写命令和需要一致快照的读命令都持有独占锁。

调度在事务内重新读取 Pod 阶段和节点余量，不只依赖事务外快照。第二个实例
获得锁后看到 Pod 已完成，直接返回空执行列表。重复调用 `schedule` 不会再次
扣减资源或生成账本版本。

## 确定性

- 节点、Pod、注入、轮次、消费和账本排序均显式定义。
- 资源只使用整数。
- 打分使用 Fraction；每项固定 `ROUND_HALF_UP` 量化，总分等于量化项之和。
- JSON 使用排序键、紧凑分隔符、UTF-8 和固定换行。
- 输出不包含 wall clock、随机数、绝对路径或数据库自动 ID。
- SQLite 只影响持久化，不参与裁决顺序或摘要。
