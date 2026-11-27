# 数字出版平台多维资源争用裁决器

这是一个只依赖 Python 3 标准库的本地可重复演示项目。它模拟章节更新高峰期四类后台作业对节点 CPU 份额、内存和 IO 带宽的争用，并保留完整的紧迫度依据、准入决策、容量变更、业务影响与检查点。

## 快速开始

环境要求：Linux / WSL Ubuntu，Python 3.10 及以上；不需要第三方包、数据库、网络或外部服务。

```bash
# 一键生成 seed=3 的样本并跑完整个逻辑事件流
python3 main.py

# 输出完整 JSON，便于独立参考裁决器比对
python3 main.py --seed 3 --json > artifacts/result.json

# 运行全部单元测试
python3 -m unittest -v
```

`seed=3` 的示例会覆盖 `ADMIT`、`PREEMPT`、`DOWNGRADE`、`REJECT` 四类决策。指定其他整数种子会得到另一份完全确定性的样本；同一种子在不同进程中重建的作业工程和裁决结果一致。

常用命令：

```bash
# 保存可复现的作业工程
python3 main.py --seed 3 --dump-workload artifacts/workload.json

# 在逻辑时刻 10 写检查点，并继续跑完
python3 main.py --seed 3 \
  --checkpoint-at 10 \
  --checkpoint-out artifacts/checkpoint.json

# 只写到检查点
python3 main.py --seed 3 --checkpoint-at 10 --stop-at-checkpoint

# 从检查点恢复并继续
python3 main.py --resume artifacts/checkpoint.json --json > artifacts/resumed.json

# 加载已有作业工程，跳过样本生成
python3 main.py --load-workload artifacts/workload.json
```

## 作业工程契约

`python3 main.py --generate-only --seed 3` 会输出完整 JSON。核心字段如下。

节点声明：

```json
{
  "node_id": "node-a",
  "capacity": {"cpu": 14, "memory": 26, "io": 33},
  "categories": ["章节渲染", "预热预取"]
}
```

作业工程：

```json
{
  "job_id": "job-001",
  "category": "章节渲染",
  "category_code": "chapter_render",
  "priority": 5,
  "preemptible": true,
  "demand": {"cpu": 1, "memory": 5, "io": 4},
  "duration": 5,
  "deadline": 14,
  "submit_time": 0,
  "mutex_group": "edition-2026-11-a",
  "downgrade_plan": {
    "profile": "eco-1",
    "demand": {"cpu": 0, "memory": 3, "io": 3},
    "release": {"cpu": 1, "memory": 2, "io": 1}
  }
}
```

约束：

- 业务类别固定为 `章节渲染`、`音视频转码`、`索引重建`、`预热预取`。
- 优先级为 1-5，5 最高。
- `preemptible` 是“可让步”标记。
- `demand`、`capacity`、`release` 都是三个整数维度：`cpu`、`memory`、`io`。
- `downgrade_plan.release` 必须恰好等于原始需求减降级需求；每个降级预案只能使用一次。
- `events` 按 `(at, sequence)` 严格升序，包含 `submit` 与可选的 `cancel` 事件。
- 完成事件不需要预先写入，由引擎按 `admit_time + duration` 确定性生成。

## 裁决口径

### 逻辑时刻顺序

每个逻辑时刻严格按以下顺序处理：

1. 到期完成：释放全部三维占用，记录 `COMPLETE` 容量变更。
2. 取消事件：释放活跃作业，记录 `CANCEL` 容量变更。
3. 同一时刻的提交事件：按紧迫度从高到低逐条裁决。
4. 输出该时刻每个节点的 `used / free / capacity` 快照。

容量边界使用闭区间：`free >= demand` 的每个维度都成立时即可准入；恰好相等必须准入，`ADMIT` 结果为确定性的。

### 紧迫度

令 `remaining = deadline - now`：

```text
remaining > 0:
  deadline_pressure = max(0, 10000 - remaining)
  urgency_score = priority * 10001 + deadline_pressure
  state = ACTIVE

remaining <= 0:
  urgency_score = -1000000 - min(overdue, 999999)
  effective_priority = 0
  state = EXPIRED
```

排序键为：

```text
[-state_rank, -urgency_score, -effective_priority, remaining, submit_time, job_id]
```

其中 `ACTIVE` 的 `state_rank=1`，`EXPIRED` 的 `state_rank=0`。因此：

- 剩余时间越短、优先级越高，越先裁决。
- `remaining == 0` 立即进入独立的 `EXPIRED` 状态，不会和 `remaining > 0` 的普通作业混淆。
- 到期作业以 `DEADLINE_EXPIRED` 拒绝，不参与抢占或降级。
- 完整的逐条依据在结果的 `urgency_table` 中。

### 节点选择与争用动作

1. 只考虑声明接纳该业务类别的节点，按 `node_id` 字典序检查。
2. 有余量且没有同组互斥冲突时直接 `ADMIT`。
3. 否则按紧迫度从低到高检查该节点上的活跃可让步作业：
   - 有未使用降级预案且释放量对当前缺口有帮助时，执行 `DOWNGRADE`。
   - 无可用降级预案时优先尝试迁移到另一可容纳节点；动作类型为 `PREEMPT`，`action=MIGRATE`。
   - 无法迁移时执行驱逐；动作类型为 `PREEMPT`，`action=EVICT`。
4. 每一步释放后立即回写节点账本并重新判断准入；同组互斥冲突不能只靠降级解决，必须迁移或驱逐。
5. 只有严格低于请求作业紧迫度的作业才可让步；紧迫度相同不抢占。
6. 若所有候选节点都无法通过低紧迫度让步腾出容量，则拒绝请求作业，且不保留试探性变更。

降级动作完成后，后续同逻辑时刻提交的作业直接使用更新后的账本，不会复用旧的余量判断。`free + release == demand` 时按闭区间规则准入，该边界有单元测试覆盖。

## 输出与可追溯性

JSON 结果主要包含：

- `decisions`：按 `(at, sequence)` 严格排序的 `ADMIT`、`PREEMPT`、`DOWNGRADE`、`REJECT` 决策。每一步包含触发原因、节点、作业、紧迫度与关联作业。
- `capacity_changes`：每次容量变化都带 `before`、`delta`、`after`、`cause`。决策触发的变更含 `decision_id`；完成/取消触发的变更含 `event_id`。
- `urgency_table`：每条待裁决作业的 `state`、`remaining`、`urgency_score`、`sort_key` 与最终决策。
- `samples`：每个逻辑采样时刻各节点的三维余量、活跃作业和活跃分配明细。
- `summary.by_category`：按业务类别统计资源满足率、截止达成数/失守数、降级/抢占/拒绝数。
- `summary.deadline_misses`：失守作业从提交到截止期间的争用决策链、竞争作业与根因。

资源满足率口径：对每个已处理作业，取其有效分配向量（普通形态为原始需求、降级形态为降级需求、抢占/拒绝为 0），按类别汇总实际分配刻度除以需求刻度；同时提供逐维满足率与逐作业比例。

## 检查点

检查点是带版本和内容摘要的 JSON：

```json
{
  "format": "publication-scheduler-checkpoint",
  "checkpoint_version": "1.0",
  "content_sha256": "...",
  "content": {
    "workload": {},
    "state": {
      "current_time": 10,
      "event_index": 11,
      "node_used": {},
      "active": {},
      "job_state": {},
      "decisions": [],
      "capacity_changes": [],
      "snapshots": []
    }
  }
}
```

加载时会校验：

- `format` 和 `checkpoint_version`；
- `content_sha256`（对规范化 JSON 内容计算 SHA-256）；
- 作业工程摘要；
- 恢复后的账本守恒、互斥与事件位置合法性。

恢复后继续推进得到的决策、容量变更、快照和紧迫度表与不中断运行完全一致。

## 单元测试覆盖

```bash
python3 -m unittest -v
```

测试包括：

- 固定种子重建一致性；
- 容量恰好等于需求时的确定性准入；
- 降级释放恰好补足缺口时的准入与同逻辑时刻账本重算；
- 迁移优先、抢占、互斥组隔离；
- 每个采样时刻的账本守恒与独立日志重放审计；
- 到期状态的独立降级语义；
- 检查点事件时刻/空档时刻恢复后与全量运行逐项一致；
- 检查点版本和摘要篡改拒绝。

