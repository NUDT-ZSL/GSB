# 本地 Kubernetes 调度仿真引擎

这是一个纯本地、无容器依赖的确定性调度仿真器。它读取 JSON 节点、Pod、调度配置和故障注入清单，依次执行过滤插件链、打分插件链与绑定提交，并用 SQLite 保存可恢复的轮次、资源、拓扑和决策状态。

所有调度时间都来自注入的逻辑时钟；代码不读取系统墙钟。固定输入、固定数据库初态下，过滤/打分/绑定结果和最终报告均可逐字节复现。

## 环境与快速开始

要求 Python 3.10+、NumPy、pytest 和标准库 SQLite：

```bash
python3 --version
python3 -m pip install numpy pytest
python3 -c "import sqlite3; print(sqlite3.sqlite_version)"
```

Linux/macOS：

```bash
SAMPLE=samples/deterministic
DB=/tmp/kubesim/state.sqlite

python3 -m kubesim load \
  --db "$DB" \
  --nodes "$SAMPLE/nodes.json" \
  --pods "$SAMPLE/pods.json" \
  --config "$SAMPLE/schedule_config.json" \
  --injections "$SAMPLE/injections.json"

python3 -m kubesim schedule --db "$DB" --output /tmp/kubesim/report.json
python3 -m kubesim explain --db "$DB" --pod db-primary
python3 -m kubesim status --db "$DB"
python3 -m kubesim verify --db "$DB" --report /tmp/kubesim/report.json
```

Windows 下使用相同的 `python -m kubesim ...` 子命令，仅需替换路径。文件锁在 Unix 使用 `fcntl`，在 Windows 使用 `msvcrt`。

也可以安装为命令：

```bash
python3 -m pip install -e .
kubesim --help
```

## 数据契约

顶层 JSON 可以是数组，也可以是带对应键的对象。样本位于 `samples/deterministic/`。

### nodes.json

```json
{
  "nodes": [
    {
      "name": "n1",
      "allocatable": {
        "cpu_millicores": 4000,
        "memory_mib": 8192,
        "gpu": 1
      },
      "labels": {
        "topology.kubernetes.io/zone": "z-a",
        "rack": "r1",
        "gpu": "true"
      },
      "taints": [
        {"key": "dedicated", "value": "gpu", "effect": "NoSchedule"}
      ]
    }
  ]
}
```

- 节点可分配 CPU 必须是正整数毫核，内存必须是正整数 MiB，GPU 必须是非负整数。
- 污点 effect 只允许 `NoSchedule`、`PreferNoSchedule`、`NoExecute`；过滤阶段淘汰前两者中的 `NoSchedule` 和 `NoExecute`。
- 同时支持常用简写和 Kubernetes 资源键，例如 `cpu`、`memory`、`nvidia.com/gpu`、`500m`、`512Mi`。整数 `cpu_millicores` 始终按毫核解释。
- 拓扑域标签可以直接放在 `labels` 中；`topology_domains` 对象也会合并到 `labels`。

### pods.json

```json
{
  "pods": [
    {
      "name": "db-primary",
      "priority": 100,
      "workload": "database",
      "labels": {"app": "db"},
      "resources": {
        "cpu_millicores": 500,
        "memory_mib": 512,
        "gpu": 1
      },
      "node_affinity": {
        "required": [
          {"key": "gpu", "operator": "In", "values": ["true"]}
        ],
        "preferred": [
          {"key": "tier", "operator": "In", "values": ["fast"], "weight": 2}
        ]
      },
      "pod_anti_affinity": [
        {
          "topology_key": "topology.kubernetes.io/zone",
          "match_labels": {"app": "cache"}
        }
      ],
      "topology_spread": [
        {
          "topology_key": "topology.kubernetes.io/zone",
          "max_skew": 1,
          "min_domains": 2,
          "when_unsatisfiable": "DoNotSchedule",
          "match_labels": {"app": "db"}
        }
      ],
      "tolerations": [
        {
          "key": "maintenance",
          "operator": "Equal",
          "value": "yes",
          "effect": "PreferNoSchedule"
        }
      ]
    }
  ]
}
```

节点亲和、Pod 反亲和和拓扑散布同时接受规范字段和常见 Kubernetes 驼峰结构，例如 `requiredDuringSchedulingIgnoredDuringExecution`、`labelSelector`、`topologySpreadConstraints`。Pod 名全局唯一；`workload` 缺省时依次取标签 `workload`、`app`、Pod 名。

### schedule_config.json

```json
{
  "filters": [
    "resource_feasibility",
    "taint_toleration",
    "node_affinity",
    "pod_anti_affinity",
    "topology_spread"
  ],
  "scores": {
    "resource_bin_packing": 0.4,
    "node_affinity": 0.3,
    "topology_balance": 0.2
  },
  "total_time_budget_ms": 40,
  "max_concurrent_per_workload": 2,
  "max_parallel_pods": 2,
  "retry_budget": 2,
  "retry_delay_ms": 1,
  "logical_clock_step_ms": 1,
  "precision_tolerance": 0.000001,
  "weight_decimals": 12,
  "score_decimals": 6,
  "weight_rounding": "ROUND_HALF_EVEN"
}
```

过滤插件顺序固定为资源、污点、节点亲和、Pod 反亲和、拓扑散布。配置只决定哪些插件启用，不改变该顺序。打分权重可省略，未给出的已知插件权重为 0；未知插件名会被拒绝。

每个插件的逻辑耗时默认取 `logical_clock_step_ms`，可通过 `plugin_costs_ms.filters`、`plugin_costs_ms.scores` 和 `plugin_costs_ms.bind` 覆盖。`max_parallel_pods` 是全局并发槽，`max_concurrent_per_workload` 是同一 `workload` 的并发槽。

### injections.json

```json
{
  "injections": [
    {
      "type": "latency",
      "pod": "web-a",
      "phase": "filter",
      "plugin": "topology_spread",
      "ms": 3
    },
    {
      "type": "terminate",
      "pod": "db-primary",
      "phase": "bind",
      "exit_code": 137
    },
    {
      "type": "abandon",
      "pod": "cache-b",
      "phase": "after_filter",
      "reason": "operator cancelled"
    }
  ]
}
```

- `latency` 增加指定阶段的逻辑耗时。
- `terminate` 在阶段检查点写入 SQLite 后调用 `os._exit`，用于真实进程强制终止测试。
- `abandon` 产生“已启动、未绑定”的主动放弃结局。
- 终止注入对已完成阶段检查点的 Pod 只生效一次；`resume` 重放时不再次注入同一崩溃点。

## 调度算法

### 过滤

每个 Pod 对每个节点执行启用插件，即使某插件已失败，后续插件仍会计算并保留结果。每个节点输出五个插件各自的 `passed/reason/detail`，以及首个失败插件的固定顺序号 `1..5`。

- 资源可行性使用整数毫核、整数 MiB 和整数 GPU 卡数直接比较，不做未换算浮点比较。
- 污点容忍匹配 key、value、effect；`Exists` 可省略 value。
- 节点亲和 required 项之间为 OR，同一项内表达式为 AND。
- Pod 反亲和按候选节点拓扑域和已提交 Pod 标签判定。
- 拓扑散布在放置后重新计算 `max(counts)-min(counts)`，检查 `max_skew` 和 `min_domains`。

过滤结果和节点顺序均按节点名稳定排序。

### 打分

三个原始分均为 `[0, 100]`：

- `resource_bin_packing`：`100 * mean((capacity - remaining + request) / capacity)`，包含有容量的 GPU 维度；越充分使用节点越有利于减少碎片。
- `node_affinity`：无 preferred 规则时为 100；否则为 `100 * matched_weight / total_weight`。
- `topology_balance`：每个散布约束使用 `100 / (1 + skew_after)`，再对约束取平均。

权重归一化流程：

1. 将输入权重视为精确十进制数。
2. 精确除以权重和。
3. 按 `weight_decimals` 和 `ROUND_HALF_EVEN` 或 `ROUND_HALF_UP` 量化。
4. 逐插件记录 `rounding_direction`（`UP`、`DOWN`、`EXACT`）和量化绝对误差。
5. 原始分按 `score_decimals` 量化，归一化中间值保留完整乘积，最终总分再量化。
6. 输出每项原始分、归一化权重、中间值、误差上界和 NumPy `float64` 交叉校验值。

累计误差上界覆盖原始分量化、归一化权重量化和总分量化。若前两名总分差小于 `precision_tolerance`，节点按字典序打破平局，并输出 `precision_sensitive=true` 与完整误差来源链。

### 并行与预算

调度器是确定性离散事件引擎，配合 `ThreadPoolExecutor` 执行实际并行的过滤/打分计算：

- 同一逻辑时刻最多启动 `max_parallel_pods` 个 Pod，同一 `workload` 不超过其并发上限。
- 启动顺序为优先级降序、Pod 名升序。
- 逻辑时钟只在配置的阶段耗时和注入延迟上推进。
- `clock >= start + budget` 后不再启动新的 Pod；剩余从未启动的 Pod 标记为 `BUDGET_UNSTARTED`。
- 已经启动的 Pod 必须完成过滤/打分/绑定或得到明确失败、主动放弃结论，不会因预算被截断。报告记录实际 `end_ms` 和 `overrun_ms`。
- 资源版本或拓扑约束在并行提交时冲突，会消耗 `retry_budget` 并基于最新持久化状态重算。

结局包括：

- `BOUND`：成功提交。
- `FILTER_FAILED`：已启动，但所有节点被过滤。
- `BUDGET_UNSTARTED`：预算内未启动。
- `ACTIVE_ABANDON`：主动放弃或重试预算耗尽。
- `BIND_FAILED`：不可重试的绑定错误。

## 持久化与一致性

SQLite 使用 WAL、`BEGIN IMMEDIATE`、`synchronous=FULL` 和进程级文件锁。

主要表：

- `nodes`：容量、剩余资源、静态标签/污点、`resource_version`。
- `pods`：规范化 Pod 规格、版本摘要、当前阶段。
- `rounds`：轮次水位、输入摘要、状态、逻辑时钟、最终报告摘要。
- `round_pods`：每 Pod 阶段、草稿/最终决策链、结局、摘要。
- `bindings`：唯一绑定账本、实际扣减资源、前后资源版本、拓扑增量。
- `topology_domains`：拓扑域 Pod 计数和版本。
- `round_snapshots`：仅完整提交轮次发布的资源与拓扑快照。

绑定事务同时完成以下操作：

1. 校验轮次输入摘要。
2. 校验 Pod/版本唯一性和同优先级顺序。
3. 校验 `resource_version`，拒绝过期快照。
4. 再次校验资源、Pod 反亲和和拓扑散布。
5. 原子扣减 CPU/内存/GPU，更新拓扑域计数。
6. 写入绑定账本和最终决策摘要。

同一 Pod、轮次、版本重复提交相同绑定返回幂等结果；相同键但不同节点或资源返回 `E_IDEMPOTENCY_CONFLICT`。任何一步失败都会回滚整个事务。

读取方使用 `round_snapshots`。若尚无完整提交轮次，`status` 返回初始容量而不会暴露运行中轮次的中间扣减。

### 恢复

每一阶段都会以独立事务保存 `round_pods.stage` 和决策草稿摘要。`resume` 的规则：

- 已有绑定且已终态的 Pod 直接跳过，绝不重复扣减。
- 非终态 Pod 清空草稿并从过滤阶段重放。
- 曾启动过的 Pod 在恢复时不受新启动预算限制，必须得到明确结论。
- 同一终止注入不会在重放中再次触发。
- 只有所有 Pod 终态后才在一个事务内发布报告、轮次状态和快照。

因此不存在“资源已扣减但绑定未提交”或“报告已发布但决策未完成”的合法状态。

## CLI

```text
kubesim load     --db DB --nodes N --pods P --config C [--injections I]
kubesim schedule --db DB [N P C I] [--round-id ID] [--output REPORT]
kubesim resume   --db DB [--round-id ID] [--output REPORT]
kubesim explain  --db DB --pod POD [--round-id ID]
kubesim status   --db DB
kubesim verify   --db DB [--report REPORT]
```

- `schedule` 对完全相同的样本具有批次幂等性；第二次调用返回同一已提交轮次和逐字节相同报告。
- `explain` 输出指定 Pod 的完整过滤链、打分字段、误差来源、精度标记和最终节点。
- `status` 输出最新完整快照的节点剩余资源、拓扑计数和逻辑时钟水位。
- `verify` 校验 SQLite 完整性、资源守恒、绑定请求一致、拓扑计数、报告摘要、快照、决策与绑定对应关系。

业务错误写 stderr 并以退出码 2 返回，例如：

```json
{
  "error": {
    "code": "E_BAD_NODE",
    "message": "nodes[0].cpu must be positive",
    "details": {"where": "nodes[0]"}
  },
  "ok": false
}
```

常用错误码包括 `E_BAD_NODE`、`E_BAD_POD`、`E_BAD_CONFIG`、`E_BAD_INJECTION`、`E_ROUND_DIGEST_MISMATCH`、`E_STALE_RESOURCE_VERSION`、`E_IDEMPOTENCY_CONFLICT` 和 `E_ROUND_INCOMPLETE`。

## 测试与实际验证

```bash
python3 -m pytest -q
```

当前结果：`20 passed`。

测试覆盖：

- 节点、Pod、配置和注入校验；非法输入不创建数据库。
- 五插件过滤链、稳定排序、首个淘汰原因。
- 非整数权重和、舍入方向、累计误差界、精度敏感平局。
- 预算未启动、已启动过滤失败、主动放弃。
- 两次独立数据库运行报告逐字节一致；同库重复调度幂等。
- 过滤、打分、绑定三个阶段强制 `os._exit(137)` 后恢复。
- 双进程同时调度同一批次；单轮次、单绑定、无重复扣减。
- 过期资源版本拒绝、重复提交幂等、完整快照读取隔离。

固定样本两次独立运行的实际报告 SHA-256：

```text
635c56c2bf45ceed6e8d014996e9fdaad828b6d721d19e4a996786b7d9d96ed6
```

报告 `round_id` 为 `round-af1911884e19b39a`，`content_digest` 为 `7ca8df94c3fcc731f6354a8402561360bdc9db33fcdf67b275f1d88de855f68b`。样本轮次结果：

```json
{
  "bound": 8,
  "filter_failed": 1,
  "budget_unstarted": 1,
  "active_abandon": 0,
  "bind_failed": 0,
  "total": 10,
  "logical_end_ms": 45,
  "budget_ms": 40,
  "overrun_ms": 5
}
```

`verify` 的 8 项检查均为 `ok=true`：SQLite 完整性、节点资源守恒、绑定与 Pod 请求一致、拓扑计数、已提交报告、轮次快照、绑定决策对应和外部报告一致性。

## 目录

```text
kubesim/              引擎与 CLI
  models.py           解析、校验、规范化模型
  filters.py          五级过滤链
  scoring.py          打分、归一化、误差界
  scheduler.py        逻辑时钟并行调度与恢复
  store.py            SQLite 事务、快照、校验
samples/deterministic 固定演示样本
tests/                20 个行为测试
```
