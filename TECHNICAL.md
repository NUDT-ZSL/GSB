# 技术说明

## 1. 模块划分

| 文件 | 职责 |
| --- | --- |
| `shardroute/rules.py` | 严格解析逻辑表、分片边界、物理映射和查询模板；提供取模/范围路由算术 |
| `shardroute/sample.py` | 校验样本 JSON、规则摘要、物理 SQLite 文件、表结构、生成列和物理数据落片 |
| `shardroute/router.py` | 计算目标分片、裁剪分片键谓词、替换物理表名、生成稳定改写摘要 |
| `shardroute/state.py` | 状态库 Schema、规则指针、应用事务、快照读取、文件锁 |
| `shardroute/engine.py` | 查询状态机、逐分片执行、确定性合并、异常注入、恢复 |
| `shardroute/injections.py` | 注入清单校验和按查询/参数/分片匹配 |
| `shardroute/verify.py` | 只读一致性校验 |
| `shardroute/cli.py` | 七个子命令和稳定 JSON 输出 |

## 2. 样本与校验

样本顶层为：

```json
{
  "format_version": 1,
  "initial_version": "r1",
  "rule_versions": [],
  "query_templates": []
}
```

每个规则版本是完整快照，包含 `logical_tables`、`physical_mappings`、`digest`、`effective_clock_ms`、`rollback_target`。摘要以规范化 JSON 序列化后计算：

```text
sha256(canonical_json({"logical_tables": ..., "physical_mappings": ...}))
```

`load_sample()` 在创建状态库之前完成所有校验：

1. JSON 不允许重复键。
2. 逻辑表名唯一，分片键列必须存在于逻辑表结构。
3. 取模分片数至少为 1；范围边界数量为 `shard_count+1` 且严格递增。
4. 每个分片必须有一个 `primary`，最多一个 `standby`；不允许重复角色。
5. 每个物理数据库必须以只读方式打开，物理表、逻辑列、生成列均须存在。
6. 主表内实际分片键值必须路由回映射的分片。
7. 规则摘要必须匹配；查询模板必须引用有效逻辑表。

因此非法样本不会创建状态数据库，也不会修改已有状态。

## 3. 路由与改写

改写输入为不可变 `RuleSnapshot`、查询模板和参数。输出目标分片、每个分片对应的物理库/表/SQL 及统一摘要。

路由规则：

- 取模等值：`value % shard_count`。
- 取模 IN：值去重、排序后按余数分组。
- 取模范围：对每个余数计算落在范围内的首尾值，谓词形如 `col >= lower AND col <= upper AND col % n = shard_id`。
- 范围等值/IN：在相邻边界区间 `[b[i], b[i+1])` 内查找。
- 范围区间：先与全局域相交，再逐分片裁剪为 `col >= max(start,b[i]) AND col <= min(end,b[i+1]-1)`。
- 全路由：模板显式 `route_kind=all`，输出 `full_route=true`，目标为全部分片。

物理 SQL 会：

1. 用完整标识符边界匹配替换逻辑表名。
2. 用该分片裁剪后的谓词替换 `{{shard_predicate}}`。
3. 保证没有遗留占位符。

稳定排序来源：目标分片升序、分片列表升序、参数键规范化排序、JSON `sort_keys=True`。相同规则版本和参数重复改写得到完全相同字节。

## 4. 执行与合并

每个目标分片独立打开主库 SQLite 只读连接。单个分片失败不会终止其他分片，结局写入 `shard_outcomes`：

```text
SUCCESS | FAILED
database, table, shard_id, result_digest, rows_returned, error_code, error_details
```

`sum_count` 模板要求每个分片返回一行整数 `total_amount`、`row_count`。合并按 `shard_id` 升序累加 Python 整数，避免浮点误差。

普通查询汇总所有成功分片的数据行，按模板 `order_by` 和规范化整行 JSON 排序。整体结局：

- 全部成功：`SUCCESS`，提交完整结果。
- 部分失败：`PARTIAL`，提交成功分片结果，顶层码 `E_PARTIAL_SHARD_FAILURE`，同时保留 `error_codes` 和各分片结局。
- 全部失败：不提交结果；若失败码唯一则顶层直接返回该码，否则返回 `E_ALL_SHARDS_FAILED`。

## 5. 规则热更新

状态库中 `rule_versions` 保存完整不可变快照，`rule_pointer` 始终只指向一个版本。`apply` 在 `BEGIN IMMEDIATE` 事务中：

1. 读取当前指针与实际规则摘要。
2. 拒绝回滚目标非空、旧版本重新激活、生效时钟倒退。
3. 写入新 activation。
4. 原子更新唯一指针及 generation。

查询在单个读事务中读取指针与完整快照，随后在内存中冻结 `RuleSnapshot`。规则更新不会删除旧快照，因此进行中的查询始终使用开始时的版本。并发读只能看到旧指针或新指针，不会看到跨版本混合状态。

状态库使用 WAL、`busy_timeout`、外键和显式事务；`apply`/`load` 另有状态文件旁路 `fcntl` 建议锁。

## 6. 异常注入

清单按 `stage + template_id + params + shard_id` 精确匹配，只影响命中查询：

| 阶段 | 固定错误码 |
| --- | --- |
| `route` | `E_RULE_VERSION_CONFLICT` |
| `rewrite` | `E_SHARD_KEY_OUT_OF_RANGE` |
| `execute` | `E_PHYSICAL_TABLE_MISSING` |
| `merge` | `E_RESULT_DIGEST_MISMATCH` |

错误对象固定包含 `code`、`message`、`stage`、`query_template`、`target_shards`、`rule_version`、`details`。执行阶段注入只伪造该分片的失败结局，不修改样本文件或源码。合并阶段注入比较期望摘要和受控伪造摘要，并附带全部分片结局。

## 7. 持久化状态与恢复

核心表：

- `meta`：Schema 版本、样本摘要、样本根目录、查询模板目录、最后逻辑时钟。
- `rule_versions`：完整规则快照。
- `rule_pointer`：唯一当前版本指针。
- `rule_activations`：版本激活历史，满足同版本只激活一次。
- `queries`：查询请求、绑定规则版本、状态、改写结果、错误。
- `shard_outcomes`：逐分片结局。
- `query_staging`：合并完成但未提交的结果。
- `query_results`：已提交结果与摘要。
- `audit_log`：阶段审计事件。

查询状态机：

```text
STARTED -> ROUTED -> REWRITTEN -> EXECUTING -> EXECUTED -> MERGED -> COMMITTED
                                                             \-> FAILED
```

测试钩子可在 `after_route`、`after_rewrite`、`after_execute`、`after_merge` 持久化对应阶段后直接 `os._exit(75)`。

`resume` 只扫描未终态查询，在一个事务中：

1. 重新确认状态未被其他恢复进程抢占。
2. 删除该查询的分片结局和 staging 临时结果。
3. 重置为 `STARTED` 并记录 `QUERY_RESUME_CLEAN` 审计。
4. 从 `queries.request_json` 恢复模板与参数。
5. 从查询绑定的不可变规则版本重新执行。

已提交查询不在扫描集合内，因此恢复不会重复提交；重复执行 `resume` 返回空的 `completed`。恢复不需要读取样本 JSON 或注入清单，只依赖状态库和物理 SQLite 数据。

## 8. 并发与幂等

查询键：

```text
query_key = hash(template_id, params)
query_id  = hash(query_key, rule_version)
```

`queries(query_key, rule_version)` 和 `query_results(query_key, rule_version)` 均有唯一约束。

- 同查询同版本并发时，只有一个进程插入并执行，其余进程短暂等待，最终读取同一已提交结果。
- 同规则版本并发应用由文件锁和 SQLite 写事务串行化，只产生一条 activation。
- 规则更新与查询并行时，查询读到旧或新完整快照，二者都合法。
- `apply` 同版本同摘要幂等；同版本不同摘要返回 `E_RULE_CONFLICT`。

## 9. 确定性约定

- 所有业务时间只来自 `--clock` / `SHARDROUTE_CLOCK_MS`。
- JSON 固定使用 `sort_keys=True`、UTF-8、紧凑摘要序列化。
- 分片按 ID 排序，结果行先按模板列再按整行规范 JSON 排序。
- 聚合使用整数逐分片升序累加。
- 错误和结果诊断字段顺序由稳定 JSON 序列化保证。
- SQLite 样本构建关闭 WAL，固定页大小和数据顺序，重复构建字节一致。

## 10. 实际验证

执行：

```text
$ python -m pytest
44 passed in 14.81s

$ python tools/verify_twice.py
commands_per_run: 30
identical: true
run1_digest: sha256:2e13bf492940811863627604e5df06b4498a9a9cd993cd9f07c9c861b9a42b6f
run2_digest: sha256:2e13bf492940811863627604e5df06b4498a9a9cd993cd9f07c9c861b9a42b6f
```

覆盖结果包括：

- `orders_sum_count`：12 行，金额和 780。
- `orders_agg_range`：2 行，金额和 70。
- `events_range`：目标分片 `[0,1,2]`，行键值 `1200/1500/2300`。
- `r1 -> r2`：`orders_eq(customer_id=6)` 目标从分片 2 切换为分片 0。
- 四类注入分别命中 `E_RULE_VERSION_CONFLICT`、`E_SHARD_KEY_OUT_OF_RANGE`、`E_PHYSICAL_TABLE_MISSING`、`E_RESULT_DIGEST_MISMATCH`。
- 执行阶段单分片失败返回 `PARTIAL`，未受影响查询正常成功。
- `after_execute` 与 `after_merge` 崩溃均能清理临时状态并仅提交一次。

