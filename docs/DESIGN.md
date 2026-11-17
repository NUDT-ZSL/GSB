# 引擎设计说明

## 1. 处理流水线

```text
JSON 输入
  -> 描述集/schema 检查
  -> 原始字段数上限检查
  -> 记录解析与路径规范化
  -> 重复路径、状态、版本、策略标识校验
  -> 来源目标存在性固定点裁剪
  -> 去重有向图构建
  -> 稳定环枚举
  -> 按来源版本限定作用域的传播
  -> 状态归一、降级/泄露/版本冲突判定
  -> 注入参数观测
  -> 稳定排序报告
  -> 临时文件 + os.replace 原子写出
```

所有阶段只读取输入，不修改描述集对象或源文件。

## 2. 路径与记录校验

路径解析将点分段与方括号索引统一成规范键。数字点段被视为数组下标，例如 `a.0.b` 与 `a[0].b` 都变为 `a[0].b`。

记录校验分两阶段：

1. 检查记录形状、路径语法、来源列表、状态、版本、传递标记、策略标识和规范路径唯一性。
2. 从候选有效字段集合出发，反复删除“至少一个来源不在当前有效集合”的记录，直到集合稳定。

第二阶段采用固定点裁剪，能处理“A 依赖非法 B，C 又依赖 A”的级联拒绝，且结果与遍历顺序无关。重复规范路径的所有记录都被拒绝，避免“保留第一条”造成顺序敏感。

每个拒绝记录包含 1 基 `record_index` 和结构化 `reasons`，每个 reason 带业务码 `E_RECORD`。

## 3. 图与环

每条引用建立边：

```text
target -> source        （依赖方向）
source -> target        （传播方向）
```

重复 `(target, source)` 边以计数器去重，报告 `occurrences` 和额外重复数 `duplicates`。

环枚举对所有简单环执行确定性 DFS：

- 节点按规范键排序。
- 仅从环中最小规范键出发。
- 只允许访问大于起始键的节点。
- 自环输出 `[node, node]`。
- 闭合路径按最小键旋转固定，最终按 `(长度, 路径)` 排序。

因此同一图在任意记录排列下得到相同环清单。环枚举是简单环枚举，极端高密度图可能指数增长；`--timeout-ms` 会在枚举循环中中止该描述集并返回 `E_TIMEOUT`。

## 4. 传播模型

传播以“来源声明”而不是“中间字段的有效状态”为单位。设字段 `o` 的声明状态为 `s(o)`：

- `v1` 或 `v2, transitive=false`：`s(o)` 只传给直接依赖者。
- `v2, transitive=true`：`s(o)` 传给沿传播方向可达的所有字段。

对于目标 `t`：

```text
incoming(t) = { (o, s(o)) | o 的声明状态按自身版本可到达 t }
propagated(t) = max_rank(status in incoming(t))
effective(t) = max_rank(declared(t), propagated(t))
```

等级为 `plaintext(2) > masked(1) > invisible(0)`。

这种“来源不中途改姓”的模型让中间字段的低等级声明不会无审计地覆盖上游来源；同时，中间字段自身声明仍作为独立来源按自身版本传播。`truth_sources` 是所有达到 `effective(t)` 等级的自身/到达来源的排序集合。

## 5. 违规判定

对每个到达关系 `(o, t)`：

- `s(o) == invisible`：产生一条 `E_LEAK`，目标为 `t`。
- 否则若 `rank(s(o)) > rank(declared(t))`：产生 `E_DOWNGRADE`，目标为 `t`。

同一目标和业务码的多个来源聚合为一条违规，`sources` 按来源键排序。

版本冲突按目标字段直接来源集合判定：直接来源同时出现 `v1` 与 `v2` 时产生 `E_VERSION_CONFLICT`，并输出排序后的 `source_keys`。即使存在冲突，传播仍按每条来源自己的版本计算，以便报告完整诊断。

## 6. 顺序无关性

以下结构全部以规范键排序后再参与结论：

- 字段报告；
- 依赖/传播邻接表；
- 重复边；
- 环枚举起点、邻接访问顺序和环清单；
- 传播来源；
- 违规及来源；
- 版本冲突来源；
- 拒绝记录中的重复路径序号列表。

同步标记把记录视为多重集，并规范化每个记录的 `sources` 列表顺序，因此合法记录排列不会改变标记。

## 7. 资源与并发

`ThreadPoolExecutor` 只负责并发调度。每个 worker 创建独立 `monotonic` 截止时间；所有可能扩展的工作循环都调用检查点。`simulate_delay_ms` 分片休眠并检查截止时间，用于确定性复现 `E_TIMEOUT`。

批处理使用 `executor.map` 收集任务，再自然按输入索引重排；并发度只影响调度，不影响报告语义或顺序。

## 8. 报告契约

数据集级关键字段：

```text
dataset_id, status, sync_marker, computed_sync_marker, sync_marker_ok
field_count, processed_field_count, limits
codes, rejected_records, duplicate_edges, cycles, violations
fields[].declared_status / propagated_status / effective_status / truth_sources
observations.checked / consistent / inconsistent / checks
diagnostics.commands
```

观测检查只使用描述集内记录、同步标记、注入对象和运行资源参数，包括：

- 同步标记；
- 强制环长度；
- 强制降级字段；
- 强制泄露来源；
- 强制版本混用字段；
- 深链存在性与末端有效状态；
- 无违规/无环断言；
- 超时和字段上限条件。

报告写出使用同目录临时文件，完成 `flush`、`fsync` 后 `os.replace`；替换失败时删除临时文件并保留旧报告。

## 9. 显式注入生成

`build_injected_dataset(injection, dataset_id, seed)` 从参数对象直接构造记录：

- `force_cycle_length`：构造指定长度的环，1 表示自环；
- `force_downgrade_at` / `force_downgrade_from`：构造明文来源到脱敏目标；
- `forge_version_mix_at`：构造共享目标的 `v1` 与 `v2` 来源；
- `deep_chain`：按前缀/深度或显式路径列表构造长链，并把推导出的声明路径写回输出的 `injection.deep_chain.paths`；
- `simulate_delay_ms`：保留为超时复现参数。

输出描述集只包含记录、同步标记和注入参数，不包含任何期望业务码缓存；观察一致性仍由引擎从描述集重新计算。

## 10. 边界说明

- `policy_id` 必须为非空字符串并进入逐字段报告；任务未定义跨策略违规业务码，因此引擎不臆造额外违规类型。
- 超时或超字段数时返回描述集级业务码，不宣称存在完整的逐字段传播结果。
- 环枚举列出全部简单环；资源上限是应对高密度图的保护措施。
