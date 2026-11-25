# 设计说明

## 1. 目标与边界

本组件只使用本地确定性样本和本地 SQLite，不模拟真实云控制面。它重点验证四类状态一致性问题：

1. 两代策略格式统一后，依赖边与生效时间是否可解释、可重复。
2. 上游配置变化是否只影响真实依赖闭包，不污染无关分支。
3. 实例化事务是否在拓扑、并发和资源三重约束下保持守恒。
4. 策略切换和进程强杀后，活动视图、实例绑定、账本与水位能否恢复到完整一致状态。

## 2. 分层

```text
CLI
 ├─ load      -> deterministic sample generator
 ├─ build     -> validation -> policy merge -> closure -> atomic activation
 ├─ schedule  -> topology -> concurrency/resource -> atomic commit
 ├─ recover   -> staging cleanup -> ledger repair -> job replay -> missing replay
 ├─ status    -> semantic projection
 └─ verify    -> invariant checks
```

`storage.py` 是唯一的持久化入口。调度、策略和诊断不直接修改彼此文件，均通过 SQLite 表和事务协作。

## 3. 事务边界

### 3.1 输入载入

`load` 只写 `samples/`，不创建运行时数据库。v2 元数据数据库分别位于各策略包目录内，
与分片 JSON 一起构成不可变策略包。分片哈希由 SQLite 元数据声明，读取时先算原始字节 SHA-256。

### 3.2 策略激活

```text
BEGIN IMMEDIATE
  INSERT policy_versions(state='staging')
  INSERT policy_nodes / policy_edges
  INSERT template_closures
  INSERT policy_tombstones / topology_invalidations
  INSERT invalidation_events
  INSERT/VERIFY merge_decisions
  UPDATE policy_versions SET state='committed'
  UPDATE policy_pointer
  UPDATE policy_apply_jobs SET status='applied'
COMMIT
```

这不是逐条覆盖活动行，而是“新版本 ID 的影子行 + 指针一次性切换”。任何读者通过
`policy_pointer` 和 `state='committed'` 只看到完整版本。旧版本不删除，因此已开始请求仍可读取其绑定视图。

`workspace_lock(workdir, "policy")` 使多进程策略替换串行化；SQLite 唯一键
`(policy_key, generation, source_bundle_digest)` 再保证同一包只激活一次。

### 3.3 请求认领

每轮处理先获取 `workspace_lock(workdir, "schedule-round")`，保证跨进程轮次顺序与逻辑时钟单调；
单个请求认领再获取 `request-claims` 锁并使用 `BEGIN IMMEDIATE`：

```text
request_state.status=processing
run_id=<this run>
attempt=attempt+1
policy_version_id=<run policy or prior binding>
```

其他进程若看到近期租约则跳过；租约过期或 `recover` 释放后可重认领。请求主键天然阻止重复注册。

### 3.4 成功实例化

```text
BEGIN IMMEDIATE
  re-read request and verify run/attempt lease
  verify it was not already instantiated
  reload policy version and resource ledger
  re-run concurrency/resource preflight
  INSERT instances
  INSERT instance_resources
  UPDATE resource_ledger SET used=used+required WHERE used+required<=capacity
  INSERT ledger_events(before, delta, after)
  UPDATE template_concurrency SET used=used+1 WHERE used+1<=limit
  UPDATE request_state SET completed
  UPDATE clock_watermark
  UPDATE committed_instance_watermark
COMMIT
```

预检在事务外并发执行；事务内必须重新裁决，因此“预检成功但提交时资源已被其他进程用掉”会转为拒绝，
不会超扣。`os._exit` 故障注入位于事务内、提交前，SQLite 会丢弃整个事务。

## 4. 策略合并

候选排序键：

```text
(logical_clock, policy_generation, source_rank)
```

v1 条目的 `logical_clock` 可由条目覆盖，否则继承快照；`policy_generation` 可由条目覆盖。
v2 条目继承包元数据。节点和每条边都保留来源，因此同一模板合并结果可解释到具体快照。

v1 时间换算：

```text
effective_at_ms = injected_clock_ms + relative_effective_seconds * 1000
```

即使当前样本的相对秒是整数，也保存 `scale`、`rounding`、`rounding_direction` 和
`pre_round_value`，为未来小数标度扩展保留稳定审计格式。

`merge_decisions` 保存完整候选 JSON 的规范哈希。相同 `(policy_key, logical_clock, template_name)`
再次产生不同决策时拒绝写入。增量包裁决使用包逻辑时钟作为键，避免与基础合并轮次混淆。

## 5. 闭包与失效

对模板 `T`：

```text
closure(T) = union({D} + closure(D) for D in dependencies(T))
```

DAG 已在服务清单和最终策略图上验证，因此递归可终止。

配置指纹覆盖：

- 模板自身及其闭包成员。
- 版本、依赖、标签、墓碑与墓碑原因。
- 逻辑时钟、策略代数、来源格式/版本、绝对生效时刻。

更新时：

1. 比较旧/新节点得到 `changed`。
2. 对每个 changed 模板，找到闭包含该模板的所有节点，得到 `affected`。
3. 仅重算 `affected`；其余闭包记录原样复制。
4. 若指纹变化则 `revision += 1`，否则保持 revision。
5. 通过反向依赖边枚举传播链，例如 `analytics -> report`。

该算法不会把 `analytics` 的变化传播到 `orders`，即使二者最终都依赖 `catalog`。

## 6. 拓扑、并发与资源

依赖优先的 DFS 生成检查顺序，保证“首个断点”稳定。命名空间失效和深度检查先于节点检查；
节点检查顺序为依赖在上、请求根在下。版本兼容定义为：

```text
policy_node.version >= service_template.version
```

资源计划按“根模板 + 闭包”的每模板资源求和，落到命名空间映射的节点。
观测列表按资源名排序；并发项记录目标模板在该节点的使用量。

首个约束顺序：

1. 模板并发上限。
2. 节点资源预算。

`instance_resources` 保存每个模板/资源的扣减明细；`resource_ledger` 只保存节点/资源聚合值。
`ledger_events` 是聚合值的审计链。

## 7. 调度状态机

```text
(absent)
  -> processing
     -> completed
     -> rejected
     -> started_failed
     -> abandoned
  -> not_started
```

`not_started` 由轮次预算产生，不进入资源事务。所有其他终态都从 `processing` 到达。
已终态请求在后续调度中直接返回，不再扣减。

轮次预算在事务外按请求序累计；已终态请求按原逻辑成本计入，保证中断后恢复时与不中断基线使用相同预算基线。

## 8. 恢复算法

`recover` 先修复数据库，再调度缺失项：

1. 删除 `policy_versions.state='staging'` 的孤儿版本；活动指针不变。
2. 将 `running` 调度运行标记为 `interrupted`。
3. 将 `processing` 请求释放为 `recovery_pending`，保留原 `policy_version_id`。
4. 按 `instance_resources` 聚合值修复 `resource_ledger.used`。
5. 若 `ledger_events` 聚合与实例资源不一致，按 `committed_seq` 全量重建事件链。
6. 按已提交实例修复 `template_concurrency.used`。
7. 协调实例/请求状态；实例已提交者补成 completed，完成请求缺实例者释放重放。
8. 以已提交实例最大 `committed_seq` 重建水位。
9. 重放 `policy_apply_jobs(status='pending')`；若无活动策略，重建初始策略。
10. 调用正常调度器，只处理非终态请求。

恢复记录写入 `recovery_events`，但不进入 `status.semantic_digest`，所以恢复事件本身不会影响
“最终业务状态等于不中断运行”的判定。

## 9. 一致性校验

`verify` 检查：

- `PRAGMA integrity_check`。
- 活动指针存在且指向 committed 版本，无 staging 版本。
- 闭包、闭包摘要、配置指纹、依赖摘要可从策略节点重新计算一致。
- 依赖摘要集合与策略版本保存值一致。
- 资源非负、`used <= capacity`，且等于已提交实例资源明细之和。
- 每条 `ledger_events` 满足 `after = before + delta`，最终值等于账本 used。
- 模板并发使用量等于已提交实例数量且不超限。
- 无重复实例，完成请求必有实例，实例必有完成请求。
- 请求绑定的策略版本存在且 committed。
- 无遗留 processing 请求或无恢复的 pending 策略作业。
- 水位等于最大已提交实例序号。

`status.semantic_digest` 与 `verify.semantic_digest` 使用同一语义投影，便于对比不同工作目录的恢复结果。

## 10. 并发模型

- SQLite：WAL、`synchronous=FULL`、`BEGIN IMMEDIATE`、30 秒 busy timeout。
- 文件锁：
  - `policy`：策略构建/替换。
  - `schedule-round`：跨进程轮次顺序。
  - `request-claims`：请求认领临界区。
- SQLite 约束/条件更新：
  - 请求序号、实例 ID 唯一。
  - 资源 `used+required<=capacity`。
  - 并发 `used+1<=limit_value`。
  - 策略包唯一 `(policy_key, generation, source_bundle_digest)`。

## 11. 明确假设

- “每模板并发上限”按请求目标模板计数；闭包内模板资源仍然计入节点预算。
- 策略节点版本是模板配置版本，实例化要求其不低于服务清单版本。
- 逻辑时钟容差为 100 ms；显式 `build --clock` 低于持久化水位超过容差时拒绝。
- 轮次预算按每个请求的 `logical_cost_ms` 累加，预算耗尽后的同轮请求记为 `not_started`。
- 旧策略版本永久保留，以支持进行中请求的版本绑定和事后诊断。
