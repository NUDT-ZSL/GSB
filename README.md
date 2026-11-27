# 构建节点出口路由诊断工作台

一个仅依赖 Python 标准库的领域组件与本地 HTTP 工作台，用固定种子生成双 schema
策略工程、逐事件重放线路与策略变化，并将选路结果、切换历史和历史快照持久化到
SQLite。

## 能力概览

- v1 字符串社区标签 + 绝对本地优先级，与 v2 位图社区 + 相对百分比本地优先级，
  统一归一化为同一内部模型。
- 校验前缀、构建节点、下一跳、社区位图等引用；悬空引用统一写入带
  `E_REFERENCE` 业务码的报告。
- 社区 deny/allow 规则过滤后，按 AS 路径长度升序、本地优先级降序、社区偏好权重
  降序、下一跳 ID 升序、候选 ID 升序确定最优路径与备选路径。
- 每个候选均输出是否可用、被哪条规则排除、排名与逐字段择优依据。
- 线路中断/恢复按下一跳依赖定位前缀；策略调整按候选社区命中与权重变化定位前缀；
  只重算受影响前缀。
- 两版归一结果逐字段比较，并将差异归因到 `community_representation` 或
  `local_pref_scale`，与生成器注入的已知差异逐项核对。
- SQLite 原子提交事件与受影响前缀快照；从最近已提交事件直接恢复运行态续算，
  不重复写切换序列。
- 本地 HTTP 工作台按节点、前缀展示当前或历史选路、择优依据、切换历史与收敛时刻。
  `at=` 历史视图只查询 SQLite 快照，不调用选路器。

## 快速开始

要求 Python 3.10+，无需安装第三方包。

```bash
cd /home/liang/feedback/uid42a8691e-default-488

# 1. 固定种子生成双版本工程、事件序列、已知差异与清单
python3 -m build_routing generate --out artifacts --seed 20261128

# 2. 比较两版并核对生成器注入的已知差异
python3 -m build_routing diff \
  --left artifacts/project_v1.json \
  --right artifacts/project_v2.json \
  --known artifacts/known_differences.json \
  --out artifacts/diff_report.json

# 3. 初始化 SQLite 并完整重放；已存在时从最近提交事件续算
python3 -m build_routing replay \
  --project artifacts/project_v1.json \
  --events artifacts/events.json \
  --db artifacts/workbench.db \
  --reset \
  --report artifacts/replay_report.json \
  --reference-report artifacts/reference_report.json

# 4. 启动诊断工作台
python3 -m build_routing serve \
  --db artifacts/workbench.db \
  --host 127.0.0.1 \
  --port 8000
```

打开 <http://127.0.0.1:8000/>。也可以一条命令完成生成、差异比对和重放：

```bash
python3 -m build_routing demo --out artifacts --db artifacts/workbench.db --seed 20261128
```

若数据库已初始化，`serve` 可以只传 `--db`。传入 `--project/--events` 时，`serve`
会先完成数据库初始化或续算再监听 HTTP。

## 生成物

| 文件 | 说明 |
| --- | --- |
| `artifacts/project_v1.json` | v1：社区字符串标签、绝对本地优先级 |
| `artifacts/project_v2.json` | v2：社区位图整数、百分比本地优先级 |
| `artifacts/events.json` | 12 个交错线路/策略事件，逻辑时刻 100 到 1200 |
| `artifacts/known_differences.json` | 生成器注入的两项已知差异及预期选路 |
| `artifacts/manifest.json` | 固定种子、文件 SHA-256 与差异前缀清单 |
| `artifacts/diff_report.json` | 两版逐字段差异、证据与归因核对结果 |
| `artifacts/replay_report.json` | 逐事件变化、最终状态、统计与数据库元数据 |
| `artifacts/reference_report.json` | 项目引用校验报告 |
| `artifacts/workbench.db` | 已初始化的 SQLite 工作台数据库 |

固定种子为 `20261128`。同一 seed 的工程、AS 路径与事件序列确定性可复现。

## 双 Schema 归一

v1 候选示例：

```json
{
  "local_pref": 400,
  "communities": ["pref:gold", "scope:regional"]
}
```

v2 候选示例：

```json
{
  "local_pref_pct": 40,
  "community_mask": 5
}
```

v2 的 `community_catalog` 将位映射回标签；百分比按
`round_half_up(pct * canonical_max / 100)` 转为绝对刻度。两个版本的社区集合在内部
均按标签排序，避免书写顺序产生伪差异。

引用校验覆盖：

- prefix -> build node
- build node -> region
- next hop -> region
- candidate -> next hop
- v2 位图 -> 社区目录
- 重复 ID

报告示例：

```json
{
  "code": "E_REFERENCE",
  "status": "invalid",
  "issue_count": 1,
  "issues": [
    {
      "code": "E_REFERENCE",
      "entity": "candidate",
      "entity_id": "pfx-demo:cand-demo-1",
      "field": "next_hop_id",
      "reference": "nh-missing"
    }
  ]
}
```

`load_project(path, strict=True)` 在存在悬空引用时抛出
`ReferenceValidationError`，异常中的 `report` 即上述结构。

## 选路规则

1. 候选下一跳不存在或引用错误：装载错误。
2. 下一跳线路 down：排除。
3. 命中任一 deny 社区规则，或未命中 allow 规则：排除。
4. 剩余候选使用固定全序：
   - AS 路径长度升序
   - 本地优先级降序
   - 社区偏好权重降序
   - 下一跳 ID 升序
   - 候选 ID 升序

社区偏好权重是候选社区配置权重之和。排名第一为最优路径，第二为备选路径。
`SelectionResult.to_dict()["assessments"]` 给出所有候选的排除原因与排名，
`basis.ranking`、`basis.best_reason`、`basis.backup_reason` 给出择优说明。

## 事件与增量

事件格式：

```json
{"seq": 1, "at": 100, "type": "link_down", "next_hop_id": "nh-bj-a"}
{"seq": 2, "at": 200, "type": "policy_change",
 "policy_patch": {"filters": [{"id": "deny-regional", "match": "any",
                               "communities": ["scope:regional"],
                               "action": "deny"}]}}
```

策略补丁支持：

- `community_weights`：合并权重
- `replace_community_weights`：替换全部权重
- `filters`：替换全部规则
- `add_filters` / `remove_filter_ids`

线路事件只重算引用该下一跳的前缀。策略事件先比较旧/新策略下每个候选的过滤
结果与社区权重，再只选路计算实际受影响前缀。

`ReplayEngine.recompute_incremental_policy()` 用于策略声明局部修改，返回：

- `recomputed_prefix_ids`
- `unchanged_prefix_ids`
- `changes`
- `full_recompute_match`
- `unaffected_preserved`

方法会校验未受影响前缀的状态和切换序列逐字段不变，并将每个增量结果与全量
选路结果比对。

## 两版差异归因

`compare_projects(v1, v2, known)` 比较同一前缀的最优/备选路径字段：

- `candidate_id`
- `next_hop_id`
- `as_path` / `as_path_length`
- `local_pref`
- `communities`
- `community_weight`
- `rank`

归一后候选社区集合不同归因为 `community_representation`；候选绝对本地优先级
不同归因为 `local_pref_scale`。报告同时核对：

- 已知差异前缀是否全部出现
- 是否出现未注入的差异前缀
- 每项归因是否与已知注入一致
- 生成器记录的 v1/v2 最优、备选路径是否与本次独立选路一致

## 统计口径

统计同时输出 `nodes` 与 `regions` 两级，并标记可达性、备选覆盖率与问题前缀：

- `load_error`：无候选，或全部候选存在引用装载错误。
- `all_filtered`：没有候选存活。`details` 分别给出社区过滤数与线路 down 数；
  该原因同时覆盖“全部候选不可用”的统一状态。
- `single_candidate`：仅一个候选可用，因此没有备选路径。

每个问题前缀包含 `no_available_candidate` 或 `no_backup_path` 标记。引用错误即使
未导致完全不可达，也会额外输出 `reference_loading_error`。

## SQLite 与续算

核心表：

| 表 | 说明 |
| --- | --- |
| `meta` | 项目/事件哈希、最后事件、收敛时刻 |
| `committed_events` | 已原子提交事件 |
| `prefix_current` | 当前选路状态 |
| `prefix_snapshots` | 初始及每事件受影响前缀快照 |
| `switch_events` | 最优/备选路径变化序列 |
| `runtime_state` | 引擎运行态、策略、线路状态与历史 |

一个事件的前缀当前态、快照、切换记录、运行态与 `committed_events` 在同一 SQLite
事务内提交。续算时加载 `runtime_state`，核对已提交事件日志，只应用尚未提交的事件。
`switch_events` 的唯一键 `(prefix_id, event_seq, change_kind)` 防止重复写入。

运行态恢复使用规范化项目的内容哈希校验；事件流哈希也必须在续算时一致。

## HTTP API

| 路径 | 说明 |
| --- | --- |
| `/` | 本地诊断工作台 |
| `/api/health` | HTTP、SQLite 完整性与表行数 |
| `/api/meta` | 项目、最后事件、收敛时刻 |
| `/api/nodes` | 节点与区域 |
| `/api/prefixes?node=&prefix=&at=` | 当前或历史前缀列表 |
| `/api/prefix/{prefix_id}?at=` | 详情、择优依据、切换历史 |
| `/api/snapshot?at=` | 指定逻辑时刻的持久化快照 |
| `/api/stats?at=` | 当前或历史两级统计 |
| `/api/events` | 已提交事件序列 |

历史查询选择每个前缀 `observed_at <= at` 的最大 `event_seq` 快照，响应中带有
`"source": "sqlite.prefix_snapshots"` 与 `"recomputed": false`。

## Python 集成

```python
from build_routing import (
    ReplayEngine,
    WorkbenchStore,
    compare_projects,
    load_project,
)

v1 = load_project("artifacts/project_v1.json", strict=True)
v2 = load_project("artifacts/project_v2.json", strict=True)
engine = ReplayEngine(v1, events)
engine.run()

store = WorkbenchStore("artifacts/workbench.db")
store.bootstrap(v1, events, reset=True)
historical = store.snapshot_at(250)
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖：

- 双 schema 归一及 `E_REFERENCE`
- 独立参考选路器逐事件比对
- 已知差异逐项归因
- 局部策略重算与全量重算一致
- 未受影响前缀状态/切换序列保持
- 断点续算与完整重放最终一致且切换不重复
- 历史快照在选路器被禁用的条件下仍可读取
- HTTP 页面与 API
- 固定种子确定性
