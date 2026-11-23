# Local GraphStore

一个只依赖 Python 标准库的本地图数据存储引擎，负责合并两代本地图快照、构建确定性内存索引，并在“每轮并行任务数”和“内存预算”双约束下执行并行 BFS 遍历。状态推进全部落在 SQLite 事务中；索引使用不可变版本行和整体指针切换，遍历进度使用批次水位推进，支持进程被 `SIGKILL` 后按已提交状态恢复。

## 交付内容

```text
graphstore/
  snapshot.py      # v1/v2 解析、哈希/摘要/线性索引/元数据交叉校验
  merge.py         # 节点与边裁决、来源保留、审计记录
  index.py         # 确定性统一内存索引与索引摘要
  traversal.py     # BFS、过滤、单线程真值、首差异定位
  scheduler.py     # 批次计划、线程池、预算与任务数调度
  store.py         # SQLite 事务、文件锁、版本指针、批次水位
  service.py       # 可集成 GraphStore API 与 verify
  cli.py           # load/merge/traverse/resume/status/verify
  samples.py       # 确定性两代样本生成器
scripts/
  generate_samples.py
  run_acceptance.py
samples/           # 已生成的两代样本
tests/             # 41 项 pytest 测试
artifacts/acceptance/
  summary.json
  verification.log
```

## 快速开始

```bash
# 当前仓库可直接运行。目标部署版本为 Python 3.11+；
# 本验证机仅有 3.10.12，代码未使用 3.10 之后的专属语法。
python3 -m pip install -e '.[test]'
pytest

# 重建样本
python3 scripts/generate_samples.py --out samples --force

# 清空并执行完整验收（会替换 artifacts/acceptance）
python3 scripts/run_acceptance.py --output artifacts/acceptance --force
```

最小 CLI 流程：

```bash
python3 -m graphstore merge \
  --v1 samples/v1/snapshot.json \
  --v2 samples/v2 \
  --state-db build/state.sqlite

python3 -m graphstore traverse \
  --state-db build/state.sqlite \
  --mode reach \
  --start n1 --start n4 \
  --max-depth 2 \
  --max-parallel 2 \
  --memory-budget 1000 \
  --batch-size 1

python3 -m graphstore status --state-db build/state.sqlite
python3 -m graphstore verify \
  --state-db build/state.sqlite \
  --v1 samples/v1/snapshot.json \
  --v2 samples/v2
```

`status` 与所有 CLI 结果都使用 `sort_keys=True`、紧凑分隔符输出，键顺序稳定。`--pretty` 只改变缩进，不改变排序。

## 快照格式

### v1 单文件 JSON

```json
{
  "schema": "graphstore.v1.snapshot/1",
  "snapshot_id": "sample-v1-base",
  "version": 1,
  "nodes": [
    {
      "id": "n1",
      "label": "person/engineer",
      "weight": 1.5,
      "written_seconds": 1,
      "adjacency": {
        "targets": ["n2"],
        "labels": ["follows"],
        "weights": [0.5]
      },
      "out_edges": [
        {"target": "n2", "label": "follows", "weight": 0.5}
      ]
    }
  ],
  "linear_index": ["n1"]
}
```

校验内容：

- 节点 ID 按 trim + Unicode NFC 规范化后必须唯一。
- `adjacency.targets/labels/weights` 长度一致，并与 `out_edges` 顺序内容逐项一致。
- 节点权重和边权重必须是有限数值。
- `written_seconds` 必须是非负整数，`true/false` 不算整数。
- `linear_index` 必须严格升序、无重复，且精确覆盖全部节点。
- 所有错误在打开状态库前抛出；非法快照不会创建或修改持久化状态。

### v2 分片 + SQLite 元数据

`manifest.json` 声明快照版本、逻辑时钟下限、节点数上下界和每个分片的 SHA-256：

```json
{
  "schema": "graphstore.v2.manifest/1",
  "snapshot_id": "sample-v2-incremental",
  "version": 2,
  "logical_clock_floor": 1700000060000,
  "node_count_min": 6,
  "node_count_max": 6,
  "shards": [
    {
      "shard_id": "s0001",
      "file": "shards/s0001.json",
      "sha256": "...",
      "generation": 1,
      "node_count": 4
    }
  ]
}
```

每个分片包含规范 ID、标签、权重、绝对写入毫秒、墓碑和预写入的节点摘要。`metadata.sqlite` 的 `snapshot_meta`、`shards`、`nodes` 必须与清单及分片逐项一致。校验包括：

- 分片文件实际 SHA-256 同时匹配清单和 SQLite 元数据。
- 分片代数按清单顺序单调；节点代数与其分片代数一致。
- 时间戳为非负整数，且不低于清单逻辑时钟下限。
- 同一 v2 包内节点 ID 不重复；墓碑仅允许引用 v1 已存在节点或本包非墓碑节点。
- 分片节点摘要、SQLite 节点摘要、分片节点集和元数据节点集必须一致。
- v2 实际节点数必须落在清单声明上下界内。

## 合并规则

同一节点的版本裁决键为：

```text
(logical_clock, shard_generation, source_priority, source)
```

- v1 的 `logical_clock` 是注入时钟换算后的绝对毫秒。
- v2 的 `logical_clock` 是记录中的绝对写入毫秒。
- 逻辑时钟相同先比较分片代数，再比较来源优先级：`v2 = 2`，`v1 = 1`。
- 裁决结果、完整候选列表和原因写入 `merge_decisions`。

v1 时间换算：

```text
write_ms = injected_clock_ms + written_seconds * scale_ms_per_second
```

节点审计记录保存 `raw_written_seconds`、`logical_clock_epoch_ms`、`scale_ms_per_second`、`rounding`、`pre_round_ms` 和最终 `write_ms`。

出边先按 `(target, label)` 去重，再按同样的版本键裁决权重。每个 `MergedEdge` 保留：

- 所有来源版本（`sources`、`source_versions`）；
- 胜出来源、逻辑时钟、分片代数和权重；
- 是否冲突；
- 全部候选权重及胜者标记。

`load`/`merge` 输出中的 `edge_conflicts` 会直接列出所有权重冲突、胜者和候选记录；
持久化的 `merge_decisions` 也包含同样的候选依据。

墓碑节点保留在审计集和索引摘要中，但不进入 `visible_node_ids`、标签前缀索引和遍历可见集。

## 统一内存索引

索引按节点 ID 升序、一次稳定构建轮次完成：

- `label_prefix_index`：标签所有非空前缀到节点 ID 的索引；
- `target_index`：出边目标到源节点 ID 的索引；
- `edge_label_index`：边标签到源节点 ID 的索引；
- 邻接表、可见节点集、墓碑审计集。

索引摘要覆盖节点数、可见节点数、墓碑数、边数、标签前缀集合、节点/边完整确定性载荷及三个辅助索引。摘要以规范化 JSON 的 SHA-256 表示；相同合并视图和相同输入参数重复构建得到逐字节一致的摘要。

示例摘要：

```json
{
  "node_count": 11,
  "visible_node_count": 10,
  "tombstone_count": 1,
  "edge_count": 18,
  "index_digest": "c137754dd7eefde5778d795a42b668886812feb9abde7e5eb04b92863abdd04c"
}
```

## 遍历语义

支持：

- `--mode exact --exact NODE`
- `--mode prefix --prefix PREFIX`
- `--mode reach --start NODE [...]`
- `--edge-label LABEL` 可重复，多个标签取并集；
- `--min-edge-weight` / `--max-edge-weight` 过滤边权重，权重不累加；
- `--max-depth` 最大深度，必须为正整数。

BFS 规则固定：

1. 深度 0 只包含起点；
2. 每层对已访问节点按 ID 排序展开；
3. 邻接边按 `(target, label, weight, winner_source)` 排序；
4. 同一目标只保留确定性最小的入边作为 `incoming`；
5. 层内节点按节点 ID 升序输出，已访问节点不重复；
6. 墓碑节点和缺失目标不进入结果。

每个起点同时执行索引快径和独立单线程真值路径。比较粒度是扁平化的
`(start, depth, node, source, incoming)` 行；不一致时输出首个差异的起点、层号、
并行/真值节点 ID 及各自来源快照版本。

## 预算调度

起始节点按固定 `batch_size` 分组。每个批次在启动前计算：

```text
estimate_entries = 起点 + 深度范围内按过滤条件展开的邻接条目数
```

每轮最多启动 `max_parallel_tasks` 个批次，并从剩余预算中扣除已成功、失败和正在运行的批次估计值。候选批次放不下时，该批次以及后续所有未启动批次都进入 `abandoned`，原因类似：

```text
memory_budget_exhausted: need=11 remaining=8
```

结局区分：

- `planned`：预算内但尚未启动，`status` 计为 `not_started`；
- `running`：已启动且未完成，`status` 计为 `started_in_progress`；
- `failed`：已启动后抛错并提交失败结果，`status` 计为 `started_failed`；
- `abandoned`：预算不足主动放弃；
- `succeeded`：正常完成。

已启动批次在进程内必须等待线程结束；没有线程超时取消，也不会把超时描述成已终止。

## SQLite 持久化与恢复

状态库使用 WAL、`FOREIGN KEY`、`BEGIN IMMEDIATE` 和同目录 `.lock` 文件的 `flock` 排他锁。

关键表：

- `state`：唯一 `current_index_version` 指针；
- `index_versions`：不可变索引版本及 `staging/committed/superseded` 状态；
- `index_nodes`、`index_edges`：按索引版本隔离的完整索引行；
- `merge_decisions`、`migration_decisions`：合并与迁移裁决审计；
- `traversal_runs`：运行请求、水位、真值摘要和最终结果摘要；
- `traversal_batches`：批次内容摘要、结果摘要、状态和放弃原因；
- `batch_commit_log`：每个运行批次的唯一提交记录。

索引整体替换流程：

1. 在连接私有 `TEMP TABLE` 中装载节点和边；
2. 单个 `BEGIN IMMEDIATE` 事务内插入版本化永久行和裁决记录；
3. 同一事务把 `state.current_index_version` 指向新版本，并把旧版本标为 `superseded`；
4. 提交后才对读取方可见。

因此读取方不会看到半提交索引。进程在事务中收到 `SIGKILL` 时 SQLite 回滚；临时表随连接消失；重试同一快照会命中唯一快照身份并返回 `IDEMPOTENT_REPLAY`。

遍历恢复流程：

1. 重建批次计划并逐批校验 `batch_index/starts/content_digest/estimate_entries`；
2. `planned` 与上次未提交的 `running` 批次都是待重放集合；
3. 每个成功/失败批次在同一事务内写结果、结果摘要、提交日志并推进水位；
4. 已终态批次不再重放；相同批次相同结果重复提交幂等；
5. 相同批次不同结果返回 `IDEMPOTENCY_CONFLICT`；
6. 所有批次终态后，在单事务中写入最终运行结果与摘要。

运行与索引版本绑定。`status --run-id OLD_RUN` 在索引已被替换后仍展示旧运行绑定的旧索引版本，并额外给出 `current_index_version`，不会把新索引与旧批次拼成混合视图。

## 版本变化识别

`merge` 会比较清单版本号、逻辑时钟下限、节点数上下界和分片哈希：

- 无已提交索引：`INITIAL_LOAD`
- 完全相同快照身份：`IDEMPOTENT_REPLAY` 或 `NOOP`
- 旧分片哈希全部保留且新增分片/扩大节点界：`INCREMENTAL_APPEND`
- 版本前进但分片集合变化：`FULL_REPLACE`
- 版本回退：`VERSION_ROLLBACK`
- 逻辑时钟下限回退超过容差：`CLOCK_ROLLBACK_EXCEEDED`
- 同版本不同内容：`SNAPSHOT_CONFLICT`

即使判定为增量追加，索引发布仍使用新版本整表装载和指针整体切换，不做逐条覆盖。

## 业务错误码

| 错误码 | 场景 |
|---|---|
| `NODE_NOT_FOUND` | 精确查询或遍历起点不存在/已墓碑 |
| `INVALID_EDGE_LABEL` | 边标签语法非法 |
| `INVALID_DEPTH` | 深度上限不是正整数 |
| `SHARD_HASH_MISMATCH` | v2 分片实际哈希不匹配 |
| `CLOCK_ROLLBACK_EXCEEDED` | 新快照逻辑时钟下限回退超阈值 |
| `VERSION_ROLLBACK` | 快照版本回退 |
| `CONTENT_DIGEST_MISMATCH` | 批次内容/结果摘要不匹配 |
| `IDEMPOTENCY_CONFLICT` | 同批次/同请求提交了不同结果 |
| `TRAVERSAL_MISMATCH` | 并行路径与单线程真值不一致 |
| `V1_*` | v1 JSON、ID、邻接、权重、秒数、线性索引错误 |
| `V2_*` | v2 清单、元数据、分片、代数、时间戳、墓碑、摘要错误 |

CLI 失败时向 stderr 输出：

```json
{"error":{"code":"NODE_NOT_FOUND","details":{"node_id":"n404"},"message":"visible node does not exist"}}
```

## CLI

```text
load     解析、完整校验并在内存构建，不写状态库
merge    构建并原子发布索引
traverse 创建/复用确定性运行并执行未提交批次
resume   重放指定或当前索引最新运行的未完成批次
status   输出稳定排序 JSON：索引版本、节点数、水位、放弃原因
verify   SQLite 完整性、索引重放、源码重建、批次与最终结果校验
```

常用示例：

```bash
python3 -m graphstore load --v1 samples/v1/snapshot.json --v2 samples/v2

python3 -m graphstore traverse --state-db build/state.sqlite \
  --mode reach --start n1 --start n2 --start n4 --start n7 \
  --max-depth 2 --max-parallel 2 --memory-budget 20 --batch-size 1

python3 -m graphstore traverse --state-db build/state.sqlite \
  --mode prefix --prefix person/

python3 -m graphstore verify --state-db build/state.sqlite \
  --v1 samples/v1/snapshot.json --v2 samples/v2
```

强制终止恢复验证可使用环境变量 `GRAPHSTORE_TEST_BATCH_DELAY_MS` 在批次执行前注入延迟；它只用于让验收脚本稳定地在“首批已提交、后续未提交”时发送 `SIGKILL`，不进入请求摘要或结果摘要。

## 库接口

```python
from graphstore import GraphStore, TraversalRequest

store = GraphStore("build/state.sqlite")
store.merge(
    v1_path="samples/v1/snapshot.json",
    v2_path="samples/v2",
    logical_clock_ms=1_700_000_000_000,
)

request = TraversalRequest.create(
    mode="reach",
    starts=["n1", "n2", "n4", "n7"],
    max_depth=2,
    max_parallel_tasks=2,
    memory_budget_entries=20,
    batch_size=1,
)
result = store.traverse(request)
resumed = store.resume(result["run_id"])
status = store.status(result["run_id"])
report = store.verify(run_id=result["run_id"])
```

`GraphStore.environment()` 会实际创建临时表并检查 SQLite 读写能力。

## 验证

```bash
PYTHONPATH=. pytest -q
# 41 passed

python3 scripts/run_acceptance.py --output artifacts/acceptance --force
```

完整命令与输出见 `artifacts/acceptance/verification.log`，机器可读摘要见
`artifacts/acceptance/summary.json`，摘要与复现说明见 `VERIFICATION.md`。

约束：运行时只使用 Python 标准库、本地文件、SQLite、线程和进程文件锁；不访问网络、外部图数据库、容器或线上站点。

