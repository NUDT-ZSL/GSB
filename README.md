# 文档版面阅读顺序分析

本项目从零实现一个本地、确定性、可复现的文档图像版面阅读顺序分析流水线，并将分析过程建模为每页五节点的 DAG：

```text
normalize:<page> -> edges:<page> -> costs:<page> -> solve:<page> -> metrics:<page>
```

- `normalize`：像素外接矩形按画布归一化，派生中心点、栏号、区域与嵌套路径。
- `edges`：为每块构造候选后继边，附带同栏、跨栏、跨区、上下相邻、水平重叠等特征。
- `costs`：按类型顺序、列号关系、垂直间距、水平重叠、跨区生成可复算分项。
- `solve`：在候选边上用 Held-Karp 动态规划精确求最小代价单链。
- `metrics`：生成阅读顺序、规则命中、位置偏差谱、逆序数和全量/局部一致性指标。

所有页面、块、边、代价与报告均使用稳定排序和 JSON 规范化序列化。样本随机源固定为 `numpy.random.Generator(PCG64)`，种子序列为 `[seed, page_index, 7919]`。

## 快速开始

环境已验证为 Python 3.11.18、NumPy 2.0.2。运行时不需要网络或外部服务。

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e .

# 生成确定样本、三格式标注、异常夹具与初始快照
.venv/bin/python -m layout_order generate

# 全量报告
.venv/bin/python -m layout_order run

# 基线 + 三次增量 + 全量对照 + 快照/迁移/异常/哈希自检
.venv/bin/python -m layout_order self-check

# 单元与集成测试
.venv/bin/python -m unittest discover -s tests -v
```

默认输出：

- `artifacts/data/samples.jsonl`：样本清单。
- `artifacts/data/annotations/*.v{1,2,3}.tsv`：每页三格式阅读顺序视图。
- `artifacts/data/anomalies/`：字段缺失、字段数不符、非法权重、超限资源和越界样本。
- `artifacts/snapshots/current.json`：当前整体快照。
- `artifacts/report.json`：单次运行报告。
- `artifacts/selfcheck_report.json`：完整自检报告。

## 三格式契约

标注文件使用竖线分隔、首行版本头；无版本头时按字段集分派。

| 版本 | 字段 |
|---|---|
| v1 | `block_id`, `reading_order`, `block_type`, `text_len` |
| v2 | v1 + `confidence_low`, `confidence_high`, `paragraph_role`, `order_weight` |
| v3 | v2 + `column_index`, `zone_id`, `nesting_path` |

示例：

```text
#LAYOUT_RO_V3
1|0|header|63|0|1|header|1|0|0:header|root.header
2|1|body|128|0|1|body|1|0|0:top|0.body
```

- v1 升级到 v2/v3 时，逐块逐字段记录 `completions`，包括来源 `v1_default` 与补全规则。
- v3 降级到 v1 时，对多栏嵌套字段使用 `flatten_multi_column_nesting`，对顺序权重使用 `drop_order_weight`，并逐项返回 `losses`。
- 严格降级遇到不可表达的语义时返回 `E_SEMANTIC_DRIFT`，并给出首个 `block_id`；`--allow-loss` 仅用于显式降级演示。
- 跨格式裁决先核对块集合、序列和顺序权重，再以最高版本 v3 作为规范视图。

命令行迁移示例：

```bash
.venv/bin/python -m layout_order migrate \
  --input artifacts/data/annotations/page-0001.v3.tsv \
  --to v1 --out /tmp/page-0001.v1.tsv --allow-loss
```

## 增量与快照

快照包含三个独立分区，均携带单调版本号和内容 SHA-256：

```text
block_annotations
type_order
edge_weights
```

提交始终整体替换 `current.json`，不做增量修补。缓存键由 `节点名 + page_id + 上游内容指纹 + 节点配置指纹` 计算：

- 页面几何修正：只重算该页的 `normalize/edges/costs/solve/metrics` 后缀。
- 仅块类型修正：`normalize` 失效，但几何指纹不变，`edges` 可复用。
- 权重或类型顺序变化：`normalize/edges` 全部复用，`costs/solve/metrics` 对所有页重算。
- 未受影响页面：复用全部五个已提交节点。

并发提交使用进程内锁与非阻塞 `flock`。后到者在 `E_STAGE_BUSY` 下立即返回；旧版本或同版本不同校验和返回 `E_SNAPSHOT_ROLLBACK`；内容损坏返回 `E_SNAPSHOT_CORRUPT`。

## 业务码

| 业务码 | 含义 |
|---|---|
| `E_BLOCK_COVERAGE` | 候选边未覆盖全部块或引用未知/重复块 |
| `E_CONFIG_INVALID` | 权重、惩罚、类型顺序或资源策略非法，`field` 指向配置字段 |
| `E_ORDER_INCONSISTENT` | 单链、全覆盖、边明细复算或顺序约束不一致 |
| `E_VERSION_UNSUPPORTED` | 标注版本未知 |
| `E_FORMAT_INVALID` | 行字段数、必填字段、编号/数值解析、越界顺序等错误，含首个出错行 |
| `E_SEMANTIC_DRIFT` | 跨格式或降级无法保持完整语义，含差异来源块 |
| `E_SNAPSHOT_ROLLBACK` | 旧版本覆盖或同版本内容冲突 |
| `E_SNAPSHOT_CORRUPT` | 快照结构或校验和不合法 |
| `E_STAGE_BUSY` | 已有快照提交或 stage lease 生效 |
| `E_RESOURCE_LIMIT` | 块数、比较次数或估计驻留内存超限 |

资源上限位于 `dataset_config.json -> limits`：

```json
{
  "max_blocks_per_page": 14,
  "max_edge_comparisons": 200000,
  "max_resident_memory_mb": 64,
  "limit_policy": "error"
}
```

`limit_policy=degrade` 时，候选边退化为确定性基线链并写入 `degradations[].category`，例如 `edge_comparison_budget` 或 `memory_budget`。

## 实测结果

本次在 5 页、60 块样本上完成验证：

- 29 项自动化测试全部通过。
- 完整自检 114 项检查全部通过。
- 基线候选边 666 条，所有顺序链成本均可由分项独立复算。
- 页面几何修正仅重算 `page-0001` 的 5 个节点，其余 20 个节点复用。
- 权重调整与类型顺序调整均复用全部 10 个 `normalize/edges` 节点，仅重算 15 个成本后缀节点。
- 三次增量的阅读顺序、指标和内容指纹均与修正后全量重算一致。
- 固定种子两次独立基线的报告哈希一致：
  `86e3ffb59ec7d59b6be3269d77e8db8311d921cdeb5d7070c63cc71824bfd05c`。
- 完整自检报告哈希：
  `2a08cc643828056ae9638e4cd73ed61d8f98356c1549f594a444d001c162a731`。

样本真值顺序用于评估，不参与求解。若声明代价目标与人工标注在个别跨栏图注页存在差异，报告会通过 `position_deviation`、`pairwise_inversions` 和 `exact_order_match` 显式记录，不做未声明的启发式纠正。

## 代码结构

```text
layout_order/
  annotations.py   三格式解析、写出、迁移、裁决
  cache.py         内容寻址本地缓存
  costs.py         边代价分项与复算校验
  geometry.py      归一化、候选边、覆盖与资源检查
  pipeline.py      五节点 DAG 与增量执行
  samples.py       固定种子模板与异常夹具
  selfcheck.py     完整自检与报告封装
  snapshot.py      版本快照、校验和、整体替换与 stage lease
  solver.py        精确单链求解、指标与偏差谱
tests/             29 项 unittest 回归测试
```

更细的设计说明见 `docs/ARCHITECTURE.md`。
