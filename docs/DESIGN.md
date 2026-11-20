# 技术设计

## 1. 模块边界

| 模块 | 职责 |
| --- | --- |
| `validation.py` | JSON 解析、业务校验、区域拓扑与固定点可表示范围检查 |
| `numbers.py` | 十进制精确保留、NumPy 向量化定点换算、误差上界工具 |
| `merge.py` | 三层合并、跨层一致性、派生量、近容差敏感记录 |
| `engine.py` | 单集群基础计算与跨集群裁决组合 |
| `scheduler.py` | 区域波次、逻辑时间、并发、重试和结局分类 |
| `store.py` | SQLite 模式、工作日志、事务提交、恢复、读取与校验 |
| `cli.py` | 六个子命令、稳定 JSON、错误码边界和测试钩子 |

## 2. 输入校验

解析 JSON 时使用 `parse_float=Decimal`，因此 `0.3000006` 不会被提前转换成二进制浮点。对象解析器拒绝重复键。业务校验在任何数据库连接之前完成。

主要错误码：

- `E_PARSE_JSON`、`E_DUPLICATE_KEY`
- `E_DUPLICATE_CLUSTER`、`E_DUPLICATE_SCOPE`、`E_DUPLICATE_REGION`
- `E_UNKNOWN_REGION`、`E_REGION_CYCLE`
- `E_INVALID_TYPE`、`E_INVALID_RANGE`、`E_UNKNOWN_FIELD`
- `E_MISSING_PARAM`、`E_MISSING_POLICY`
- `E_FIXED_RANGE`
- `E_STALE_VERSION`、`E_ROUND_SCOPE`、`E_WORK_CLAIM_LOST`
- `E_VERIFY_FAILED` 对应的报告 `ok=false`

集群名、非全局层作用域、区域名均要求唯一。区域依赖必须闭合、无自环、无环。超时与容量必须为正整数；采样率位于 `[0, 1]`；重试倍率和副本权重必须为正数；标度、容差和误差预算必须为正。校验还会检查定点标度乘积、派生乘法与容差量化不会超出有符号 64 位范围。

## 3. 受控定点换算

对十进制值 `v`，取精确表示：

```text
v = n / d, d = 10^k
```

乘以整数标度 `S` 后，使用 NumPy 向量执行：

```text
q, r = divmod(n*S, d)
round_up = (2r > d) or (2r == d and q is odd)
fixed = q + round_up
```

这就是半偶舍入。残差定义为 `fixed*d - n*S`：正数表示向上，负数表示向下，零表示无舍入。量化误差为 `abs(fixed*d - n*S)/(d*S)`。每条记录保留：

```json
{
  "raw": 0.3000006,
  "raw_decimal": "0.3000006",
  "scale": 1000000,
  "fixed": 300001,
  "rounding": "up",
  "rounding_mode": "half_even",
  "quantization_error": 4e-7
}
```

派生和聚合在固定点整数上完成，避免在排序、差分和容差判定中重新引入二进制浮点。

## 4. 合并与一致性

每个参数按固定顺序构造候选：

```text
global:global
region:<cluster.region>     (存在覆盖时)
cluster:<cluster.name>      (存在覆盖时)
```

最后一个候选为生效值。每项输出 `source_layer`、`source_scope`、`merge_path`、`overridden_layers`、候选定值和 `override_authorization`。授权状态：

- `consistent`：所有候选定值在容差内。
- `allowed_by_policy`：存在差异，但集群允许差异覆盖。
- `denied`：存在差异，且集群禁止差异覆盖。

任一参数为 `denied` 时，集群 `dispatch_eligible=false`，调度器将其记为 `ACTIVE_ABANDONED`。

对每个参数的所有候选对，计算 `difference_ticks`。不一致条件为：

```text
abs(fixed_left - fixed_right) > tolerance_ticks
```

每个不一致对记录层、作用域、原始值、定值、容差和带符号余量 `tolerance_ticks - difference_ticks`。

## 5. 派生量与误差传播

### 最坏链路超时

区域基线超时由全局和区域层合并得到。对目标区域 `r`：

```text
prefix(r) = max(prefix(dep) + baseline_timeout(dep)) for dep in deps(r)
worst(r, cluster) = prefix(r) + effective_timeout(cluster)
```

相同最大值时按依赖区域名稳定选择路径。输出包含依赖路径、各区域基线来源链和集群生效参数来源链。

### 有效容量

```text
effective_capacity = capacity_limit_fixed * capacity_unit_factor
```

结果标度等于容量上限标度，单位来自集群规则的目标单位。

### 加权采样率

```text
weighted_sampling_fixed = sampling_fixed * replica_weight_fixed
weighted_sampling_scale = sampling_scale * weight_scale
```

### 误差上界

定点整数运算本身精确；误差上界来自输入量化和报告所需的二进制浮点表达：

- 求和：参与项量化误差之和，加一个机器 epsilon 量级尾项。
- 乘积：`|b|*err(a) + |a|*err(b) + err(a)*err(b)`，再加机器 epsilon 尾项。
- 超时链路：各区域基线超时和集群超时量化误差之和。
- 容量：容量上限量化误差乘以整数单位因子。

每个派生量输出 `error_bound`、`error_budget` 和 `within_error_budget`。超出预算会进入 `error_budget_exceeded` 列表。

## 6. 精度敏感裁决

两类记录会进入 `precision_sensitive_records`：

1. `override_candidate_delta`：同一参数的两个覆盖候选满足 `0 < difference_ticks < tolerance_ticks`，合并选择依赖该微小差值。
2. `derived_delta`：同一区域、同优先级、相邻区域排名的两个集群，其实际参与排序的加权采样率满足 `0 < difference_ticks < tolerance_ticks`，分发排序依赖该差值。

区域排序键固定为：

```text
priority descending
weighted_sampling_rate descending
cluster name ascending
```

因此 `decision_dependency=regional_rank_by_weighted_sampling_rate` 与实际调度器一致。每条记录包含确定性 ID、涉及集群/层、定值、差值、容差、误差上界和来源链。

## 7. 分发调度

区域依赖先转换为稳定拓扑波次。同一波次内按区域分组，所有可运行集群共享全局并发槽。逻辑时间规则：

- 只有 `current_time < deadline` 时才允许启动新尝试。
- 已启动尝试按自身时延运行到结束，即使结束时间晚于 deadline。
- 首次失败后，若重试预算仍可用且当前时间未到 deadline，则重新排队。
- 重试预算耗尽记为 `STARTED_FAILED: retry_budget_exhausted`。
- 重试因时间预算未启动，记为 `STARTED_FAILED: retry_not_started_time_budget_exhausted`。
- 从未启动的集群记为 `NOT_STARTED: time_budget_exhausted_before_start`。
- 覆盖授权失败的集群不消耗并发槽，记为 `ACTIVE_ABANDONED`。

调度输出包括最大观测并发、结束逻辑时间、每集群尝试轨迹、区域分组计数和稳定事件序列。

## 8. SQLite 事务与恢复

模式核心表：

- `versions`：样本规范化快照、内容哈希、版本 ID。
- `rounds`：轮次号、状态、完整作用域、最终裁决缓存和逻辑时钟。
- `work_items`：每集群工作状态 `PENDING/CLAIMED/COMPLETE`、输入摘要和基础计算结果。
- `round_clusters`：已提交轮次的完整结果、配置摘要、派生量、精度记录和摘要哈希。
- `current_pointer`：唯一当前已提交轮次指针、版本和逻辑时钟水位。

提交协议：

1. 文件锁下创建或恢复轮次，并为完整作用域插入 `PENDING` 工作项。
2. 每个工作项通过 `BEGIN IMMEDIATE` 原子声明；计算后在独立事务中写为 `COMPLETE`。
3. 所有工作项完成后，在文件锁下进行跨集群裁决；裁决缓存写入轮次但不切换指针。
4. 一个事务内插入全部 `round_clusters`、更新逻辑时钟、`COMMITTED` 状态和 `current_pointer`。
5. `status`/`explain` 只读取指针指向的轮次；待提交轮次只在 `pending_rounds` 中显示进度。

若进程在任意步骤被终止：

- 未提交事务由 SQLite 回滚。
- 已完成工作项保持 `COMPLETE`，恢复时不会重新计算。
- `CLAIMED` 且属主进程已退出的工作项可重新声明。
- 已有裁决缓存可直接复用，仍只做一次最终指针切换。
- 已提交指针不会回退。

## 9. 并发与幂等

- 文件锁：串行化模式初始化、版本加载、轮次创建、裁决缓存和最终提交。
- SQLite：`BEGIN IMMEDIATE`、主键/唯一约束和 WAL 读快照保证工作声明、单次提交和完整读取。
- 工作项主键：`(version_id, round_id, cluster)`。
- 轮次作用域由同一版本样本稳定确定；重复 `distribute` 返回同一已提交轮次。
- 多进程可处理不相交 `--cluster` 子集；最后一个完成者触发一次提交。

## 10. 确定性

- 不读取墙钟、PID、主机名或随机数进入业务报告。
- 集群、区域、参数、精度记录和事件都有固定排序。
- JSON 以 `sort_keys=true` 输出，内部哈希使用规范紧凑 JSON。
- 浮点仅用于可审计报告和误差上界；裁决比较使用定点整数。
- 两次独立状态库运行 `load`、`distribute`、`status`、`explain`、`verify` 输出逐字一致。
