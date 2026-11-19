# 版面阅读顺序分析 — 技术设计与实验说明

## 1. 环境与复现

要求 Python ≥ 3.10、NumPy ≥ 1.24，无外部服务、无容器、无第三方数据集。

```bash
# 本机 ensurepip 缺失时使用 uv 创建虚拟环境（等价于 python -m venv）
uv venv .venv
source .venv/bin/activate
uv pip install -r requirements.txt

# 一键生成确定性样本并运行完整实验（基线 + 规模变化追踪）
python -m layout_order all

# 仅生成样本 / 仅运行实验
python -m layout_order generate
python -m layout_order run --report reports/experiment_report.json

# 测试
python -m unittest discover -s tests
```

产物：

* `data/baseline.jsonl`、`data/scale_sweep.jsonl`：确定性版面样本；
* `snapshots/`：版本化快照（`layout_stats` / `cost_weights` / `order_result`）；
* `cache/`：候选边缓存台账（build / reuse / recompute）；
* `reports/experiment_report.json`：完整实验报告。

两次干净运行的报告逐字节一致（已用 `cmp` 验证）。

## 2. 模块结构

| 模块 | 职责 |
| --- | --- |
| `generator.py` | 固定种子、固定画布（1754×1240）、四类版面族样本与真值 `reading_index` |
| `geometry.py` | 矩形归一化、1D 列聚类（列号严格自左向右）、区域与通栏特征、JSONL 校验 |
| `edges.py` | 五类候选后继边构造、确定性排序编号、分项代价打分与独立重算 |
| `solvers.py` | Held-Karp 精确解、保序分带解、贪心解与单链/全覆盖/代价一致性校验 |
| `selector.py` | 规模观测、三路径显式优先级裁决、降级标记与拒绝理由 |
| `metrics.py` | 位置偏差谱、相邻块对正确率、可承受精确参考子集 |
| `snapshots.py` | 单调版本号 + SHA-256 校验和、整体替换、缓存台账 |
| `pipeline.py` | 端到端编排与跨模块一致性自检 |
| `cli.py` | 命令行入口与 JSON 报告落盘 |

## 3. 确定性保证

1. 样本：`numpy.random.default_rng(seed)` 固定随机流，块列表经种子控制的确定性洗牌，
   `block_id` 顺序不泄漏真值；JSONL 用规范化 JSON（键排序、无空白）写入。
2. 候选边：同一 `(src, dst)` 多来源去重保留最强语义标志，最终按
   `(src block_id, dst block_id)` 升序排序并编号 `e000000 ...`，满足“同分边按
   来源块再按目标块 block_id 升序打破平局”。
3. 数值：所有浮点统一保留 9 位小数；决策只依赖结构化规模观测，不读墙钟与实际 RSS。
4. 报告：规范化 JSON 序列化；每次实验先重置运行期缓存目录，保证报告逐字节可复现。

## 4. 候选边与代价模型

边来源标志：`region_flow`、`vertical_adjacent`、`same_column`、`cross_column`、
`fallback_chain`。其中兜底边为双向全连接，保证候选图始终存在哈密顿路径，任何求解
路径的相邻块对都能在候选边集合中核对。

边代价分项（均可独立重算，总和即 `total_cost`）：

| 分项 | 含义 |
| --- | --- |
| `type` | 类型先后违规惩罚；禁配转移使用独立的高惩罚 `forbidden_penalty` |
| `column` | 同栏/通栏为 0；跨栏按列序距离计分，向左回跳额外 +1 |
| `gap` | 垂直间距（仅向下间距计费） |
| `overlap` | 水平重叠不足惩罚 `(1 - overlap)` |
| `upward` | 向上边重罚；顺读“左栏底→右栏顶”豁免（列序距离已编码代价） |

权重、惩罚、类型表非法时返回 `E_CONFIG_INVALID` 并在 `details.field` 指明字段。

## 5. 三条求解路径与取舍裁决

显式优先级（高到低）：

1. 硬资源上限不可突破（候选边数 / 比较次数 / 驻留内存）；
2. 准确率地板优先于速度：能精确就精确；
3. 分带必须满足比较预算与内存余量，否则继续降级；
4. 贪心为兜底，降级携带 `degraded=true` 与 `degradation_categories`。

| 路径 | 算法 | 规模判据（默认） | 精度特征 |
| --- | --- | --- | --- |
| `exact` | Held-Karp 动态规划，O(n²2ⁿ) | n ≤ 13 且比较/驻留预算内 | 全局最优，实验中小页 100% |
| `banded` | 先按栏、栏内垂直分带，带内精确再拼接 | 13 < n ≤ 40 且分带预算内 | 保同栏保序，实验中 100% |
| `greedy` | 最近后继分段 + 候选边内链间接龙 | 兜底 | 最快但易违反类型/栏序，实测触发地板 |

分带按物理栏切分而非水平切带，从根本上避免割裂同栏；`column_split_events`
记录带间同列事件，用于评估割裂风险。决策记录含规模观测、三路径代价估计、
拒绝理由与降级类别，且所选路径与实际求解路径在自检中强制一致。

## 6. 可核对性与参考路径

每页同时产出：

* 全页所选路径相对真值的位置偏差谱（桶 `0-0,1-1,2-2,3-4,5-7,8-12,>=13`）
  与相邻块对正确率；
* 确定性子集（页眉/标题/最大栏/页脚优先，上限 13 块）上的精确参考顺序，
  比较所选路径在子集上的偏差谱与相邻对正确率，并记录精确参考是否与真值一致；
* 子集不足两块时显式记录 `reference_available=false` 与原因
  `subset_below_two_blocks`，不允许用“无法取得参考值”掩盖可比较页。

## 7. 快照、资源上限与业务码

三类快照各维护单调递增版本号与载荷 SHA-256；内容变化即整体替换（临时文件 +
原子改名），不做增量修补；校验和不符或信封损坏返回 `E_SNAPSHOT_CORRUPT`。

| 业务码 | 触发条件 |
| --- | --- |
| `OK` | 正常 |
| `E_CONFIG_INVALID` | 权重/惩罚/阈值非法，`details.field` 指名字段 |
| `E_ORDER_INCONSISTENT` | 单链破坏、块缺失/重复、顺序边不在候选集、代价重算不符 |
| `E_ACCURACY_FLOOR` | 相邻对正确率低于 `accuracy_floor`，标明降级路径 |
| `E_SNAPSHOT_CORRUPT` | 快照/缓存信封损坏或校验和不符 |
| `E_RESOURCE_LIMIT` | 候选边数、比较次数或驻留内存超上限，含 observed/limit |
| `E_INCONSISTENT` | 跨模块自检失败，`details.category` 指名类别 |
| `E_DATA_INVALID` | 样本 JSONL 结构、矩形或真值非法 |

## 8. 实验结果（默认配置，本机实测）

* 基线 5 页：精确 2 页、分带 3 页，平均相邻对正确率 1.0，无准确率地板命中。
* 规模追踪 7 页（7→60 块）：路径依次为
  `exact, exact, banded, banded, banded, banded, greedy`；
  最大页（60 块三栏）贪心相邻对正确率 0.864，低于地板 0.9，报告码
  `E_ACCURACY_FLOOR` 并标记 `exact_too_expensive`、`banded_too_expensive`。
* 全部 12 页均取得精确参考子集（`reference_missing_pages=0`）。
* 40 个单元/集成测试通过；两次干净运行报告 `cmp` 逐字节一致。
