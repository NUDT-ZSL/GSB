# 技术设计

## 1. 状态与事务边界

SQLite 是唯一事实来源。关键表：

- `cluster_config`、`nodes`：固定候选集与选举配置。
- `cluster_state`：逻辑时钟水位、主节点、当前选举 ID、epoch。
- `leases`：每节点当前租约、租约代数、会话代数、数据版本、到期时间、定点快照。
- `lease_history`：`(node_name, lease_generation)` 主键，约束同一代数只授予一次。
- `renewals`：以确定性请求键为主的续约提交记录。
- `elections`、`precision_records`：选举结论、候选排序快照、精度敏感候选对与误差链。
- `prepared_ops`：两阶段写入的 `PREPARED` 意图，用于识别半提交。
- `audit_events`：拒绝原因审计。

一次成功的授予或续约分为：

1. 文件锁串行化写入者。
2. `BEGIN IMMEDIATE` 校验状态，计算确定性操作键，提交 `PREPARED` 意图。
3. 可选故障注入点 `after_prepare`。
4. 新事务中再次校验状态哈希，原子更新租约、历史、续约/选举、精度记录、时钟水位和 `COMMITTED` 标记。
5. 可选故障注入点 `after_final_insert`，此时事务尚未提交。
6. 提交事务。
7. 可选故障注入点 `after_commit`。

`PREPARED` 与最终业务状态永不在同一提交中混合。恢复时，若找不到对应已提交租约/续约/选举，则标记为 `CLEANED`；找到则确认为已提交。恢复路径只读取 SQLite，不使用进程内存。

## 2. 定点与精度

配置中的 `scale` 是每毫秒固定单位数，样本为 `1,000,000`。

对值 `v`：

```text
scaled = v * scale
units = floor(scaled)
residual = scaled - units, 0 <= residual < 1
```

每个转换记录 `scale`、`floor` 舍入、原始数值/浮点表示、`units`、有理残差分子/分母。整数部分单独使用的误差上界为 `<1 unit`，即 `<1/scale ms`；残差被保留后，比较误差上界为 `0`。

安全余量是精确乘积：

```text
remaining = expires_at - logical_clock
safety = lease_ms * safety_margin_ratio
margin = remaining - safety
```

内部比较使用 Python `Fraction`，不从浮点数近似重新计算。定点值用于持久化审计和稳定输出；每个节点保留自己的小于一单位的残差，因此小数时钟步进、重复续约与边界比较不会累积丢弃。

样本中：

```text
alpha margin   = 850.0000 ms
charlie margin = 850.0002 ms
gap            = 0.0002 ms = 200 fixed units
tolerance      = 0.0010 ms = 1000 fixed units
```

两者优先级和租约代数相同，数值裁决使用精确差值，`charlie` 获胜；由于 `0 < gap < tolerance`，结果为精度敏感排序。记录中包含候选对、双方得分、误差来源链、首个别否决原因和稳定次序最终兜底规则。

## 3. 选举链

候选必须同时满足：

- 当前租约状态为 `active`；
- 租约会话代数等于节点当前会话代数；
- 逻辑时钟下的剩余时长大于零；
- 剩余时长不小于精确安全余量。

排序键为：

```text
priority DESC,
lease_generation DESC,
margin_to_safety DESC,
node_name ASC
```

前三项是数值裁决，`node_name` 是稳定最终次序。若当前主节点仍可参选则保留，只有失效或低于参选安全边界时才选择新主节点。少于 `quorum` 个可参选节点时结论为主节点为空，避免不一致地继续服务。

## 4. 逻辑时钟

所有到期、续约和选举计算只使用命令传入的 `--clock`。系统墙钟没有参与业务判断。

- 请求时钟小于水位且回拨量超过 `clock_backward_tolerance_ms`：`E_CLOCK_REWIND`。
- 回拨在容差内：采用持久化水位作为有效时钟并记录 `rewind_tolerated`。
- 单步前进超过 `max_clock_step_ms`：`E_CLOCK_JUMP`。
- 成功写入后，水位、定点整数和残差一并原子持久化。
- `status --clock` 是无写入视图，不改变水位。
- 时钟错误在 `PREPARED` 之前返回，因此不会产生半状态。

## 5. 并发与恢复

并发保证来自三个独立层次：

1. 进程间文件锁：同一工作区写入串行化。
2. SQLite `BEGIN IMMEDIATE` 与 WAL：事务写入互斥，读取方只看到完整提交快照。
3. 数据库唯一约束：`lease_history(node_name, lease_generation)`、`renewals(request_key)`、`elections(election_key)` 作为最终防线。

确定性请求键包含配置哈希、节点、会话代数、租约代数、数据版本和逻辑时钟。重复请求命中 `COMMITTED` 意图或唯一记录时直接返回缓存结果，不重复写租约或选举。

恢复流程：

1. 扫描 `PREPARED` 记录，并检查对应最终记录是否存在。
2. 不存在则标记 `CLEANED`，存在则标记 `COMMITTED`。
3. 按恢复时钟重新计算每节点剩余时长与过期状态。
4. 从持久化租约快照重建选举；不采用被终止进程的内存状态。
5. 一次事务提交清理、租约快照、选举结论与时钟水位。

样本验证了 `after_prepare` 和 `after_final_insert` 均未产生已提交租约，恢复只清理意图；`after_commit` 的租约与选举完整保留。

## 6. 验证不变量

`verify` 检查：

- 配置哈希、时钟水位整数/残差一致。
- 无遗留 `PREPARED` 记录。
- 当前租约数不超过 `max_leases`。
- 当前 `(node, lease_generation)` 存在唯一历史记录。
- 当前安全余量和剩余时长的定点快照与精确重算一致。
- 主节点拥有未过期租约且满足法定人数。
- 选举记录可用其租约快照和前一主节点重放出相同结论。
- 精度敏感标志与候选对记录一致。

## 7. 复现结论

固定样本与两台干净运行的关键结果：

- 初始主节点：`bravo`。
- `1000ms` 故障转移主节点：`charlie`。
- 精度敏感候选对：`charlie` / `alpha`，差值 `0.0002ms < 0.001ms`。
- 首个别否决：`bravo`，`E_LEASE_EXPIRED`。
- `1849.9999ms` 边界候选：`alpha`、`charlie`；持久化水位保持 `1000ms`。
- 6/8 进程并发：唯一选举 ID 一个，每节点每租约代数一条历史，续约提交一条，续约 7 个幂等返回（8 进程时）。
- 强制终止退出码：`86`；`after_prepare` / `after_final_insert` 各清理一条半提交，`after_commit` 保留三条租约。
- `python -m unittest discover -v`：18 项全部通过。
- 两次 `scripts/demo.py` 输出逐字节相同。

机器可读记录见 `artifacts/verification.json`。
