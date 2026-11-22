# 架构说明

## 1. 领域对象与不变量

输入由 `models.json`、`replicas.json`、`plan.json`、补丁 JSON 和逻辑时钟脚本组成。
`hotgray.models` 在打开 SQLite 前完成所有结构校验：

- 模型版本号在清单中唯一且严格递增；
- 同一版本层名唯一，`param_count` 为正整数；
- 每层分片序号连续，字节区间从 0 开始无缝覆盖 `param_count * 4`；
- 预处理/后处理/特征表/上游模型引用闭合且类型匹配；
- 资源依赖有向图无环；
- 副本显式加载的分片摘要链必须与模型版本完全一致；
- 灰度档位编号连续、比例严格递增并以 `1.0` 收尾；
- 补丁只引用目标版本存在的层与分片，摘要、范围和目标清单一致；
- 逻辑时钟脚本内时间非递减。

不合法输入不会打开最终 SQLite 路径。初始化数据库先在临时文件中写入完整 schema 与
初始状态，再原子替换到 `state.db`。

## 2. 增量补丁

补丁条目只包含：

```json
{
  "layer": "encoder.1",
  "shard_index": 1,
  "byte_start": 128,
  "byte_end": 256,
  "digest": "sha256:..."
}
```

`compose_patch_shards` 按目标版本层和分片顺序重建摘要链：

- 补丁声明项使用补丁摘要和范围；
- 补丁未提供源路径时，从目标版本清单的对应分片路径推导；
- 未声明项强制继承基线同层同序号分片；
- 任一项与目标清单不一致时返回 `HG1004`，并带上 `version`、`layer`、
  `shard_index`、`expected`、`actual`；
- `compare_target_directories` 再把增量结果逐分片与目标整体目录字节比较。

物化使用内容寻址文件，数据库主键 `materializations.digest` 去重。未变化分片不会产生
第二个物理文件。

## 3. 失效闭包

引用图包含以下节点：

```text
model -> layer -> shard
model -> resource
resource -> resource dependency
load_cache -> layer
warmup -> load_cache + model
inference -> warmup
```

补丁提交时比较基线与目标版本：

- 分片摘要或字节范围变化产生 `shard_patch` 触发源；
- 新资源或摘要/修订/依赖/参数变化产生 `config_change` 触发源。

从每个目标缓存节点出发，按照稳定排序的依赖边做 BFS，求出到变化节点的最短路径，再反向
形成传播路径。例如：

```text
shard:2:encoder.1:1
  -> layer:2:encoder.1
  -> cache:replica-a:load_cache:2
  -> cache:replica-a:warmup:2
  -> cache:replica-a:inference:2
```

配置变化经过 `resource -> model -> warmup`，不会误伤只依赖权重分片的 `load_cache`。
基线缓存记录为 `baseline_preserved` 可复用；目标缓存若不受变化节点影响记录为
`target_unaffected` 可复用。`analyze_resource_change` 可用于独立配置变更的影响分析。

## 4. 预热与灰度状态机

副本轮次状态：

```text
pending -> loading -> prewarmed -> gray
   |          |           |          |
   +----------+-----------+----------+-> rolling_back
                                           |
                              +------------+-------------+
                              v                          v
                         rolled_back             rollback_not_achieved
```

规则：

- `patch.applied` 是开始预热的前置步骤；
- 预热启动时记录 `warmup_expected_ms` 和 `warmup_deadline_ms`；
- `at_ms > deadline` 时预热超时，触发回退；
- 预热完成前重新校验目标全部摘要和配置；不通过则触发回退；
- 第一档要求所有灰度成员处于 `prewarmed` 或 `gray`，后续档位要求所有成员为 `gray`；
- 未就绪时档位保持原值，返回并持久化每个未就绪项；
- 档位推进后新请求可走目标版本，旧请求继续使用已保存的绑定快照。

请求路由：

```text
bucket = sha256(request_id)[:16] / 2^64
target = active_gray and bucket < current_ratio
```

没有随机数、没有墙钟、没有进程本地状态。

## 5. 回退

人工中止或预热失败时：

1. 记录 `failure_at_ms` 与 `failure_at_ms + rollback_window_ms`；
2. 所有轮次副本进入 `rolling_back`；
3. 每经过一个 `rollback_step_ms` 降一档，步骤键 `rollback.level.<level>` 幂等；
4. 降到 0 档时副本恢复基线版本并进入 `rolled_back`；
5. 若 `at_ms > deadline` 仍未恢复，则进入 `rollback_not_achieved`，新请求只走基线，
   并写入 `HG1016` 诊断记录。

回退截止边界包含等于：`at_ms == deadline` 仍允许成功；`at_ms > deadline` 不再报告成功。

## 6. 事务与恢复

主要表：

| 表 | 作用 |
| --- | --- |
| `clock_state` | 逻辑时钟 watermark、容差、最后请求值 |
| `rounds` | 轮次状态、档位、失败时刻、回退截止、轮次水位 |
| `round_replicas` | 每个副本的阶段、服务版本、预热时间、流量比例 |
| `version_bindings` | 完整已提交版本组合及摘要 |
| `shards` | 已提交的逐层逐分片目录 |
| `materializations` | digest 级物化去重 |
| `round_steps` | 已完成/阻塞步骤日志 |
| `requests` | 请求绑定快照、状态与结果 |
| `invalidations` | 失效/复用裁决、触发源、传播路径 |
| `events` | 追加式事务事件流 |
| `diagnostics` | 运行期业务错误码与诊断字段 |

写操作通过跨平台文件锁串行化临界区，并使用 SQLite `BEGIN IMMEDIATE`。数据库启用 WAL、
外键和 `synchronous=FULL`。读查询使用独立快照事务，只读取 `committed = 1` 的版本绑定。

崩溃恢复不依赖“最后一步一定执行完”。每个业务步骤先检查 `round_steps`：

- 已提交步骤直接返回幂等结果；
- 未提交步骤重新执行；
- 物化文件可以已存在但数据库未提交，重放时会重新校验摘要并复用；
- `resume` 依据持久化轮次、时钟水位和步骤日志选择补丁、预热、推进或回退。

强制终止不承诺生成退出报告。正常 `resume` 才会产生恢复结果。

## 7. 并发与幂等

- 同一补丁重复提交返回 `reused=true`；
- 同一档位重复提交返回 `reused=true`；
- 相同请求 ID、副本和输入重复开始会复用原绑定；
- 相同请求 ID 但不同输入返回 `HG1018`；
- 数据库唯一键保证 `version_bindings(version)`、`round_steps(round_id, step_key)`、
  `materializations(digest)`、`requests(request_id)` 和版本绑定只提交一次；
- 内容寻址文件只写一个 digest 路径。

## 8. 工作台

`python -m hotgray serve` 提供：

- `/`：单页领域可视化；
- `/api/status`：完整稳定排序诊断快照；
- `/api/health`：轮次与逻辑时钟摘要；
- `/api/error-catalog`：业务错误码目录。

稳定排序：

```text
replicas: replica_id
shards: version, layer, shard_index
events: at_ms, id
requests: replica_id, started_at_ms, request_id
```

页面展示灰度时间轴、副本阶段、逐层分片摘要、依赖传播图、失效/复用裁决、未就绪项、
请求绑定与事务事件流。单个副本的异常不会改变其他副本的展示或推理绑定。

## 9. 错误码

| 错误码 | 名称 | 场景 |
| --- | --- | --- |
| `HG1001` | `INVALID_INPUT` | 字段结构非法 |
| `HG1002` | `VERSION_NOT_MONOTONIC` | 版本重复或非严格递增 |
| `HG1003` | `MISSING_SHARD` | 分片缺失、序号断裂、覆盖不完整 |
| `HG1004` | `SHARD_DIGEST_MISMATCH` | 摘要/字节/增量目录不一致 |
| `HG1005` | `MISSING_LAYER_REFERENCE` | 补丁或模型层引用不存在 |
| `HG1006` | `CONFIG_REFERENCE_CYCLE` | 配置引用成环 |
| `HG1007` | `INVALID_GRAY_RATIO` | 灰度比例非法 |
| `HG1008` | `CLOCK_REGRESSION` | 逻辑时钟回拨超过容差 |
| `HG1009` | `UNRESOLVED_REFERENCE` | 配置或版本引用无法解析 |
| `HG1010` | `DUPLICATE_LAYER` | 层名重复 |
| `HG1011` | `INVALID_PARAM_COUNT` | 参数量非正整数 |
| `HG1012` | `REPLICA_INVALID` | 副本清单非法 |
| `HG1013` | `PATCH_INVALID` | 补丁结构冲突 |
| `HG1014` | `TARGET_NOT_READY` | 目标组合、配置或副本未就绪 |
| `HG1015` | `ILLEGAL_STATE_TRANSITION` | 阶段迁移非法 |
| `HG1016` | `ROLLBACK_NOT_ACHIEVED` | 超出回退窗口 |
| `HG1017` | `NOT_FOUND` | 对象不存在 |
| `HG1018` | `CONFLICT` | 幂等键或状态冲突 |
| `HG1019` | `PERSISTENCE_ERROR` | SQLite 错误 |
| `HG1020` | `INVALID_CLOCK` | 时钟脚本或计划时间非法 |

错误 JSON 包含 `code`、`name`、`message`、`diagnostics` 和 `context`；已经出现的
`version`、`layer`、`shard_index`、`replica_id`、`round_id`、`resource_id` 等字段会被
提升到 `diagnostics`。
