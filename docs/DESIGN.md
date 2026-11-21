# 设计说明

## 模块边界

- `validation.py`：包发现、严格 JSON、v1/v2 schema、哈希、代数、依赖图。
- `models.py`：不可变候选、规范化内容摘要、版本自然排序与首个冲突理由。
- `view.py`：跨格式提案报告、时间线选择、四态判定、参数来源。
- `events.py`：事件流校验、内容引用/内嵌解析、双源冲突与审计。
- `db.py`：SQLite schema、事务、文件锁、操作步骤日志、视图与水位。
- `engine.py`：load/merge/reconcile/resume/status/verify 编排。
- `cli.py`：稳定 JSON 输出与退出码。

## 持久化实体

| 表 | 作用 |
| --- | --- |
| `source_candidates` | v1/v2 原始候选，保留相对/绝对时间语义 |
| `migration_decisions` | v1 相对时间唯一迁移决策 |
| `config_versions` | 已物化不可变版本，含来源、摘要、参数、墓碑 |
| `event_streams` | 两源独立已提交序号和水位时钟 |
| `event_commits` | 每个 `(source, seq)` 的不可重复事件审计 |
| `operations` / `operation_steps` | 命令级恢复日志 |
| `views` | 不可变完整视图快照与哈希 |
| `published_recovery_snapshots` | 发布事务内固化的水位、迁移、状态迁移与时钟 |
| `state_transitions` | 服务状态/生效版本变化审计 |
| `recovery_decisions` | resume 开始/完成裁决 |
| `clock_state` | 逻辑时钟水位与容差 |
| `package_loads` | 成功加载审计 |

所有 JSON 列均使用规范化 JSON（键排序、无空格、UTF-8、禁 NaN），因此相同
输入得到相同哈希。

## 视图构建

视图输入仅为 `accepted=1` 的已物化版本。对每个
`(service, environment)`：

1. 按 `(generation, logical_clock, effective_at, priority, version, digest)`
   建立时间线。
2. 分离 `effective_at <= clock` 与未来版本。
3. 已生效时间线最后一项决定当前状态：
   - 墓碑 -> `tombstoned`
   - 过期 -> `expired`
   - 其他 -> `active`
4. 无已生效项但有未来项 -> `pending`。
5. 生效项进入 `effective_services`；墓碑永不进入。

每个参数在 `param_provenance` 中保存：

- `source_format`、`source_version`、`source_priority`、`content_digest`。
- `original_time`：v1 相对秒数/标度/舍入/换算后毫秒，或 v2 绝对毫秒。
- `decision_basis`：选择当前生效代数的确定性规则。

## 事件提交

一个事件步骤在同一 SQLite 事务中：

1. 检查 `(source, seq)` 是否已提交。
2. 解析内容并校验摘要。
3. 检查目标已声明、代数不回退、同代冲突、依赖上游与环。
4. 写入/撤销候选接受标记。
5. 写 `event_commits`、流水位与事件审计。

否决事件也推进其流水位并保留完整事件 JSON 与拒绝理由，避免同一坏事件
永久阻塞后续服务。结构性序号冲突不推进水位，作为步骤错误记录，其他流仍
可继续。

## 恢复协议

命令请求体哈希决定 `op_id`。步骤计划在首次执行前持久化；步骤效果和
`state=committed` 在同一事务提交。恢复时：

1. 加载请求中的包路径，重算包摘要；摘要变化则拒绝恢复，避免重放不同输入。
2. 读取操作日志和已完成步骤。
3. 从第一个未完成步骤继续。
4. 事件/候选/迁移插入均为唯一键幂等。
5. 最终原子发布视图并标记操作完成。

发布事务使 `views`、时钟、状态迁移和操作完成标记一起可见。事件事实在
发布前已经完整提交，但尚不会被任何“最新视图”引用；恢复只需补完剩余步骤。
同一事务还写 `published_recovery_snapshots`，把视图版本、双流水位、迁移
决策、状态迁移和时钟水位固化为可校验恢复包。

## 锁顺序

为避免死锁，多锁按名称排序获取：

```text
op:<op_id>
publish
stream:<source>
merge:<package_digest>
```

实际上各命令只取需要的子集；`publish` 只在最终短事务持有。OS 在进程
`SIGKILL` 后自动释放所有 `fcntl` 锁。

## 已知边界

- SQLite 仍是单写者；并发实例可以并行解析、校验和准备，但物理写事务由
  SQLite 串行化。这是可靠性与本地零依赖的权衡。
- `resume` 只恢复被中断的操作；因业务校验失败进入 `failed` 的操作需要修正
  输入后使用新的包摘要/请求，不会自动重试永久性错误。
- v1 的可选 `expires_at_ms` 是扩展字段；固定样本主要通过 v2 演示过期。
- 同一环境依赖解析是刻意收紧的规则，便于本地多环境隔离与确定性验证。

