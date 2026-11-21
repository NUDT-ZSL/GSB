# 本地调度插件仿真与诊断工作台

这是一个完全离线、确定性的调度插件链仿真器。它不连接真实集群，不创建容器，
只读取本地 JSON 样本，在 SQLite 中维护节点资源账本、Pod 阶段、轮次水位、
注入消费水位、逻辑时钟和不可变决策归属账本。

核心目标不是优化打分公式，而是保证下面三件事在异常注入和进程强杀后仍然一致：

- 每个 Pod 的绑定或淘汰都能归属到具体轮次、插件、节点和输入摘要。
- 资源扣减与归属条目在同一事务中提交，不存在悬挂扣减或重复扣减。
- 对某个 Pod/轮次的注入只改变该 Pod 的归属条目，其他 Pod 的资源与已提交归属不被改写。

## 快速开始

环境要求：Python 3.10+（建议 3.11）、SQLite（Python 标准库自带）。

```bash
python -m pip install -r requirements.txt
```

Linux/macOS：

```bash
python -m scheduler_diag load --bundle samples/main --db var/workbench.sqlite --reset
python -m scheduler_diag schedule --bundle samples/main --db var/workbench.sqlite
python -m scheduler_diag status --bundle samples/main --db var/workbench.sqlite
python -m scheduler_diag inject --bundle samples/main --db var/workbench.sqlite
python -m scheduler_diag verify --bundle samples/main --db var/workbench.sqlite
```

Windows PowerShell：

```powershell
python -m scheduler_diag load --bundle samples/main --db var/workbench.sqlite --reset
python -m scheduler_diag schedule --bundle samples/main --db var/workbench.sqlite
python -m scheduler_diag status --bundle samples/main --db var/workbench.sqlite
python -m scheduler_diag inject --bundle samples/main --db var/workbench.sqlite
python -m scheduler_diag verify --bundle samples/main --db var/workbench.sqlite
```

也可以执行完整演示和重复性检查：

```bash
./scripts/demo.sh
./scripts/repeatability.sh
```

```powershell
.\scripts\demo.ps1
.\scripts\repeatability.ps1
```

演示报告写入 `reports/`，SQLite 与锁文件写入 `var/`。

## CLI

| 命令 | 作用 |
| --- | --- |
| `load` | 先完整校验四个 JSON 文件，再初始化 SQLite；非法输入不会创建或修改状态。`--reset` 在验证成功后重建账本纪元。 |
| `schedule` | 按轮次、优先级降序、Pod 名升序调度全部 pending Pod；每个 Pod 一个事务。 |
| `resume` | 先回放校验摘要、账本链和注入水位，再只处理未提交 Pod。 |
| `inject` | 列出注入计划、消费顺序、归属版本和错误码；`--id` 过滤单个注入。 |
| `status` | 以排序 JSON 输出节点余量、Pod 归属摘要、轮次、注入消费和逻辑时钟。 |
| `verify` | 同时执行账本回放与资源对账，检查负余量、重复扣减、悬挂扣减、悬挂归属和摘要不一致。 |
| `replay` | 额外诊断命令：校验摘要、账本链和注入水位后输出完整归属账本。 |

所有命令支持 `--out PATH` 原子写入稳定 JSON。`schedule`/`resume` 的
`--crash-at filter|score|bind` 是诊断开关，会在事务提交前直接 `exit(97)`，
用于验证强杀恢复；正常运行时不要使用。

## 样本格式

`--bundle` 目录必须包含 `nodes.json`、`pods.json`、`config.json`、
`injections.json`。完整样本见 `samples/main/`。

节点：

```json
{
  "name": "node-a",
  "cpu_millicores": 4000,
  "memory_mib": 8192,
  "labels": {"zone": "a", "disk": "ssd"},
  "taints": [
    {"key": "dedicated", "value": "gpu", "effect": "NoSchedule"}
  ]
}
```

Pod 支持整数资源、优先级、到达轮次、标签、节点亲和/反亲和、Pod 反亲和、
拓扑散布和容忍声明。`node_affinity`/`node_anti_affinity` 均使用
`required`/`preferred` 表达式列表；Pod 反亲和规则格式为：

```json
{
  "label_selector": {"app": "web"},
  "topology_key": "zone"
}
```

拓扑散布格式为：

```json
{
  "max_skew": 1,
  "topology_key": "zone",
  "when_unsatisfiable": "DoNotSchedule",
  "label_selector": {"app": "api"}
}
```

配置：

```json
{
  "filter_plugins": [
    "ResourceFit",
    "TaintTolerance",
    "NodeAffinity",
    "PodAntiAffinity",
    "TopologySpread"
  ],
  "score_weights": {
    "ResourceBinPack": 2.5,
    "NodeAffinityMatch": 1.25,
    "TopologySpreadBalance": 1.75
  },
  "round_time_budget_ms": 5000,
  "retry_budget": 3,
  "logical_clock": {"start_ms": 0, "plugin_tick_ms": 1}
}
```

过滤插件列表必须覆盖全部五个已声明过滤插件，执行顺序会规范化为上表顺序。
打分权重必须覆盖全部三个打分插件，单个权重非负且总和大于零。
`retry_budget` 表示遇到 SQLite `busy/locked` 时整笔 Pod 事务的最大重试次数；
正常多实例路径由文件锁先行串行化。

注入：

```json
{
  "id": "inj-weight-r3",
  "round": 3,
  "pod": "r3-weight-injected",
  "phase": "score",
  "type": "invalid_weight",
  "node": "node-a",
  "plugin": "ResourceBinPack",
  "message": "controlled invalid scoring weight",
  "details": {"weight": -1}
}
```

支持 `node_label_conflict`、`resource_overcommit`、`invalid_weight`、
`plugin_missing`。每个 Pod/轮次最多一个注入。注入的目标 Pod 会得到独立失败
归属条目，同轮其他 Pod 继续调度。

## 业务错误码

输入校验错误：

- `E_BAD_NODE`：节点字段、资源、标签、污点或名称唯一性非法。
- `E_BAD_POD`：Pod 字段、整数资源、亲和、散布、容忍或名称唯一性非法。
- `E_BAD_CONFIG`：插件覆盖、权重、预算或逻辑时钟配置非法。
- `E_BAD_INJECT`：注入 ID、轮次、阶段、目标节点/插件或类型非法。
- `E_BAD_BUNDLE`：样本目录或 JSON 文件不可读/不可解析。

运行期注入错误：

- `E_INJECT_NODE_LABEL_CONFLICT`
- `E_INJECT_RESOURCE_OVERCOMMIT`
- `E_INJECT_INVALID_WEIGHT`
- `E_INJECT_PLUGIN_MISSING`

账本与状态错误：

- `E_DIGEST_MISMATCH`：样本摘要与持久化清单或账本条目不一致。
- `E_LEDGER_CORRUPT`：账本版本链、条目摘要或注入水位不一致。
- `E_VERIFY_FAILED`：资源账本与归属条目无法一一对应。
- `E_NOT_LOADED`、`E_STATE_EXISTS`、`E_ALREADY_COMMITTED`、
  `E_RESOURCE_RACE`、`E_BUSY`：状态生命周期和并发保护错误。
- `E_NO_FEASIBLE_NODE`、`E_FILTER_*`、`E_TIME_BUDGET`：普通裁决结果。

## 归属账本

每个 Pod 提交一个不可变条目，绑定条目包含：

- `round`、`pod`、`input_digest`、`input_summary`
- `status`、`plugin`、`selected_node`、`error_code`
- `before_resources`、`after_resources`
- 每个被淘汰节点的 `plugin`、`node`、`error_code`、首个 `evidence`、
  `source_field`
- 所有可行节点的 `raw_scores`、`weights`、`normalized_terms`、`total_score`
- `injection_consumed`、`injection_watermark`
- `entry_digest`

SQLite 中每次提交写入一个完整的下一版本账本快照，然后原子替换
`ledger_pointer`。旧 `ledger_versions`、`allocations` 和已完成的
`pod_states` 由触发器禁止更新/删除。

## 事务与恢复

单个 Pod 的整个提交流程在一个 `BEGIN IMMEDIATE` 事务中完成：

1. 读取当前节点余量、Pod 阶段、已绑定 Pod 和注入水位。
2. 设置阶段为 `filtering`/`scoring`/`binding`。
3. 执行过滤、打分和注入归属。
4. 绑定路径扣减节点资源并插入 `allocations`。
5. 生成下一版账本快照、更新指针、Pod 终态、注入消费和逻辑时钟。
6. 若该轮全部 Pod 完成，同事务推进轮次水位。
7. 提交事务。

进程在任意步骤被强杀时，SQLite 回滚未提交事务；下一进程打开数据库后
只看到上一个已提交版本。多实例通过数据库旁的独占文件锁串行化调度，
SQLite `BEGIN IMMEDIATE`、唯一约束和资源条件更新提供第二层保护。

## 过滤与打分定义

过滤链固定为：资源可行性 → 污点容忍 → 节点亲和/反亲和 → Pod 反亲和 →
拓扑散布。资源比较只使用整数毫核和 MiB，不做浮点累加。

打分原始分范围均为 0..1000：

- `ResourceBinPack`：放置后 CPU/内存利用率（千分比）的平均值，偏向装满节点。
- `NodeAffinityMatch`：匹配的 preferred 权重占比；无 preferred 时为 1000。
- `TopologySpreadBalance`：每个散布约束按放置后偏斜计算
  `max(0, 1000 - skew * 250)`，多约束取平均；无约束时为 1000。

若原始分为 `r_p`、权重为 `w_p`、权重总和为 `W`，则：

```text
normalized_term_p = r_p * w_p / W
total_score = sum(normalized_term_p)
```

内部使用 `Fraction` 做精确有理数运算。每个归一化项按 `ROUND_HALF_UP`
量化到 6 位小数，总分是这些量化项之和，因此展示值可以直接相加复核。
三个打分插件场景的绝对误差上界为 `0.0000015`。总分相同时按节点名升序决胜。

## 测试与验证

```bash
python -m unittest discover -s tests -v
```

测试覆盖：

- 输入唯一性、整数资源、污点/容忍、配置覆盖、注入引用和非法输入零持久化。
- 五个过滤插件、整数资源边界、首个证据来源。
- 三插件原始分/权重/归一化中间值/总分、非整数权重和同分节点名决胜。
- 四种注入的独立错误码、消费水位和同轮控制 Pod 正常绑定。
- 强杀后的账本回滚、资源不重复扣减、逐字节一致的恢复结果。
- 双实例并发只绑定一次、重复 `schedule` 幂等。
- 摘要不匹配、注入水位篡改、旧账本/分配/终态不可变。
- 两个全新数据库的 `load`、`schedule`、`status`、`verify`、`inject` 输出逐字节一致。

实际验证结果与样本输出摘要见 `docs/verification.md`。
