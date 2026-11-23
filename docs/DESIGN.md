# 服务伸缩决策组件技术说明

## 1. 目标与范围

组件面向本地平台升级与兼容场景，把同一组服务在两代策略格式（v1 扁平
JSON、v2 分片 JSON + SQLite 元数据）下的伸缩决策统一为一条可恢复、可复算、
多进程安全的裁决流水线。全部输入由 `scripts/generate_samples.py` 确定性生成，
运行不依赖网络、容器或外部服务。

## 2. 目录结构

| 路径 | 职责 |
| --- | --- |
| `scaler/errors.py` | 稳定业务错误码与异常层次 |
| `scaler/models.py` | 服务、节点、两代策略、统一视图、结论的数据类 |
| `scaler/parser.py` | 全部输入的解析与严格只读校验 |
| `scaler/merger.py` | 跨版本策略合并、v1 相对秒到绝对毫秒换算 |
| `scaler/topology.py` | 依赖拓扑序、上下游失效闭包 |
| `scaler/fixedpoint.py` | 定点标度、舍入登记、误差上界与敏感判定 |
| `scaler/placement.py` | CPU/内存双预算落点、原子失败、淘汰候选 |
| `scaler/engine.py` | 负载选档、副本量与成本定点计算、结论生成 |
| `scaler/scheduler.py` | 逻辑时钟预算 + 故障域并发的并行调度 |
| `scaler/storage.py` | SQLite schema、事务、跨平台文件锁 |
| `scaler/orchestrator.py` | 包版本裁决、reconcile/resume/status/verify、补丁闭包 |
| `scaler/cli.py` | `load/reconcile/resume/status/verify` 命令行 |
| `scripts/generate_samples.py` | 确定性样本与 v2 元数据库生成 |
| `scripts/run_demo.py` | 离线端到端验收演示 |
| `tests/` | 33 个 `unittest` 用例，覆盖八类业务要求 |

## 3. 错误码

| 错误码 | 触发条件 |
| --- | --- |
| `E_BAD_SAMPLE` | 服务/节点/v1/负载/配置校验失败、依赖环、自环 |
| `E_BAD_SHARD` | v2 分片哈希、代数、绝对毫秒或元数据库不一致 |
| `E_VERSION_ROLLBACK` | 策略版本号或策略代数下限回退 |
| `E_BUDGET_EXHAUSTED` | 逻辑时间预算无法容纳下一动作 |
| `E_PLACEMENT_UNSATISFIABLE` | 双资源预算下无法整批放置副本 |
| `E_STATE_CONFLICT` | 同版本不同哈希、轮次/动作冲突 |
| `E_CORRUPT_STATE` | 持久化轮次或账目缺失、负余量 |

所有解析错误在写入任何 SQLite 或登记文件之前抛出（见
`tests/test_no_write_on_failure.py`）。

## 4. 两代策略合并

每个服务的胜出版本按以下顺序裁决：

1. 声明逻辑时钟更高者胜；
2. 时钟相同时来源版本优先级 `v2 > v1`；
3. v2 内部再比较策略代数。

统一视图的每个档位保留 `source_version`、`generation`、`logical_clock`、
`raw_time`、`decision_basis`、`conversion_scale`、`rounding`。v1 的
`relative_seconds` 在注入逻辑时钟下按
`absolute_ms = injected_clock_ms + relative_seconds * 1000` 换算，标度固定
1000，舍入方向逐档登记。v2 墓碑服务在调度阶段直接判定为主动放弃，不消耗
逻辑时间预算。

## 5. 并行预算裁决与三类结局

调度器使用确定性的逻辑时钟模拟并行运行时，不依赖墙钟或线程：

- 服务按 Kahn 拓扑序（优先级、名称稳定破并）就绪；
- 每个故障域维护活跃动作数，受 `fd_concurrency` 约束；
- 动作启动前在一个 `BEGIN IMMEDIATE` 事务内同时完成动作认领与预算预留，
  这是真实预算判定，而非事后标注；
- 已启动动作必须执行完毕，预算耗尽后先排空在途动作，再把剩余动作登记为
  未启动。

结局严格区分：

| 结局 | 含义 |
| --- | --- |
| `finished/*` | 已启动并执行完毕（含 `scaled_in/scaled_out/noop/failed/abandoned`） |
| `not_started/budget_exhausted` | 预算内无法启动 |
| `not_started/abandoned` | 主动放弃（墓碑、依赖阻塞等） |

`failed` 仅用于启动后落点失败，且失败不部分提交任何节点资源。

## 6. 双资源落点与缩容先行

`NodePool.place` 仅在拓扑域匹配的节点上做首适配贪心，先在临时副本上推演整批
副本；任一副本放不下即整体抛 `E_PLACEMENT_UNSATISFIABLE`，带回全部被淘汰候选
节点与首个触发约束（`cpu`/`mem`/`topology`），真实节点余量不变。同故障域内
缩容动作排序先于扩容并先释放资源，避免动作顺序制造的虚假不足。池子总赤字通过
`numpy` 整数向量聚合。

## 7. 定点账本与误差传播

所有请求副本量、有效 CPU/内存、成本增量、预算余量按 `fixed_scale` 量化为整数
tick，全程不使用二进制浮点参与决策。每次 `convert/add/sub/mul` 生成单调序号的
登记条目，记录原始值、舍入方向（`up/down/exact`）、被舍弃余量与累计误差上界。
当两个量化值之差扣除双侧误差上界后仍小于 `tolerance_ticks`，且该差值参与裁决
时，服务标记为精度敏感，并输出包含服务名、登记序号与层级路径的误差来源链。

## 8. 补丁失效闭包与结论复用

补丁支持档位比例、依赖关系与墓碑三类修改。根节点集合沿依赖图向下游传播得到
受影响闭包；闭包内的结论、落点、精度标记全部标记重算，闭包外服务标记复用且
不触发重算。复用时从 SQLite 读回原结论并原样写回，保证逐字不变
（`tests/test_patch.py` 校验闭包内外集合与持久化结论稳定性）。

## 9. 版本替换、原子写与崩溃恢复

新策略包依据版本号、策略代数下限与分片哈希集合判定：

- 版本回退或代数下限回退 -> `E_VERSION_ROLLBACK`；
- 同版本同哈希 -> `no_op`（幂等）；
- 旧哈希是新哈希真子集 -> `incremental_append`；
- 其余 -> `full_replace`。

轮次水位、副本数、节点占用、精度记录与逻辑时钟水位在单个事务内写入，配合
WAL 与 `synchronous=FULL`。强制终止后，已提交动作不会重放；只有 `pending`
动作进入 `resume`，由于其节点账目从未提交，重放恰好一次且不会重复扣减。

## 10. 多进程一致性

`Storage` 在 SQLite 侧使用 `BEGIN IMMEDIATE` 写事务、WAL 与 busy timeout，
在进程侧通过 sidecar 锁文件协同：Windows 走 `msvcrt.locking`，POSIX 走
`fcntl.flock`。`claim_action` 的唯一约束 `(round_no, service)` 与原子预算
预留保证同一服务同一轮次同一版本只被裁决一次、节点 CPU/内存不被超卖，重复
提交直接返回 `duplicate`。

## 11. 命令行

```bash
python scripts/generate_samples.py samples/data
python -m scaler --db state.db --samples samples/data load
python -m scaler --db state.db --samples samples/data reconcile
python -m scaler --db state.db --samples data reconcile --failure-domain fd-a
python -m scaler --db state.db --samples samples/data resume
python -m scaler --db state.db --samples samples/data status
python -m scaler --db state.db --samples samples/data verify
```

`status` 以服务名、节点名稳定排序输出 JSON；`verify` 从输入重算结论与账本并
与持久化状态比对，发现不一致时退出码为 2。

## 12. 验证结果

在当前容器（Python 3.10.12、numpy 1.26.4、SQLite 3.37.2）执行：

```bash
python -m unittest discover -s tests -v
python scripts/run_demo.py
```

- 33 个单元/集成用例全部通过，覆盖解析错误码、两代合并、预算放弃、落点原子
  失败、定点舍入、失效闭包、崩溃恢复、版本回退与三进程故障域并发；
- `run_demo.py` 观察到真实扩容落点、容量不足启动后失败、墓碑主动放弃、崩溃
  后单次重放以及 `verify.ok = true`。

## 13. 环境说明

任务建议 Python 3.11+，当前容器仅提供 Python 3.10.12。代码仅使用 3.10 已具备
的标准库能力（dataclasses、sqlite3、fcntl/msvcrt 分支、decimal），在 Windows
Python 3.11 上同样可运行；部署到 Windows 时确认 `python` 与 `pip` 在 PATH 中，
并执行 `pip install -r requirements.txt`。
