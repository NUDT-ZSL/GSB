# 搜索前链路查询理解与意图解析

从零构建的确定性查询理解组件，覆盖归一化、分词、span 槽位、层级意图、点击伪相关反馈、离线指标、规模取舍、跨路径可比性、预算准入、版本快照和四阶段异常注入。全部实验基于本地固定样本，不依赖 Docker，也不需要在线数据。

当前固定样本的验收结果：两路径意图一致率 `1.0`、槽位偏移差异 `0`、最大置信偏差 `0.0`、小规模与大规模 NDCG@3 `0.917021`、MRR `1.0`；小规模选择精确路径，576 条查询时切换为受限候选近似路径。完整结果见 `reports/experiment_report.md` 和 `reports/experiment_report.json`。

## 快速开始

要求 CPython 3.11 与 NumPy 1.26.4。仓库不提交 `.venv`，首次运行如下：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/generate_data.py
.venv/bin/python -m query_understanding run
.venv/bin/python -m unittest discover -s tests -v
```

当前执行环境为 CPython `3.11.15`、NumPy `1.26.4`，工作区具备持久化写入权限。样本生成器固定随机种子 `20261122`，再次运行会得到逐字节一致的数据和 `manifest.json`。

## 目录

```text
query_understanding/   核心实现与 CLI
data/                  固定样本、标签体系、词典、映射、配置、指标口径
scripts/               数据生成与实验入口
tests/                 30 项 unittest 测试
reports/               可复现 JSON/Markdown 报告与快照演示
```

常用命令：

```bash
.venv/bin/python -m query_understanding process --query "蘋果 手機 黑色"
.venv/bin/python -m query_understanding compare --queries data/query_log_small.jsonl
.venv/bin/python -m query_understanding scale --queries data/query_log_large.jsonl
```

## 数据契约

`query_log_small.jsonl` 有 96 条查询，`query_log_large.jsonl` 有 576 条查询。每行字段：

```json
{"query_id":"q000001","query_text":"苹果 手机 黑色","session_id":"s00001","timestamp_ms":1792000000000,"locale":"zh-CN","raw_clicks":[{"doc_id":"q000001-d1","rank":1,"dwell_ms":930,"domain":"ecommerce","intent_class":"product_search"}]}
```

- `data/taxonomy.json`：5 个 domain（含 `general` 兜底）、12 个业务 `intent_class`、槽位 schema、日期/金额/订单号等正则。
- `data/dictionary.json`：228 条词典项，区分 `slot_value` 与 `intent_cue`，可一词多义。
- `data/mappings.json`：繁简映射与全半角映射，`traditional_sources` 声明应覆盖的繁体源字符。
- `data/metrics_contract.json`：反馈窗口、评估窗口、相关性增益、置信口径和重算要求。
- `data/manifest.json`：固定种子、生成器版本和全部数据文件 SHA-256。

点击分两窗：rank `<=2` 且 dwell `>=300ms` 只用于伪相关反馈；rank `3..5` 作为离线评估标签，避免反馈与评估共用同一窗口。相关性等级为 `min(3, 点击次数)`，增益 `2^grade-1`，折扣 `log2(rank+1)`。

## 核心设计

- 归一化：逐码点应用全半角映射与繁简映射，ASCII 小写化并折叠空白。映射缺项返回 `E_MAP_MISSING`。
- 分词：领域词典 trie 最长匹配，输出规范串码点区间 `[start,end)`；词典上限触发截断时返回 `E_DICT_TRUNCATED`。
- 槽位：词典值匹配与 schema 正则并行，置信度排序后贪心选择，严格禁止重叠且每个槽位名最多一个值；同置信度按槽位名升序破平局。
- 意图：词典证据形成 domain/intent 词法分数；stage 1 判断 top-2 margin，歧义时 stage 2 使用点击伪相关反馈确定性重排，再用固定温度 softmax 输出置信。
- 评估：逐查询记录 top-k、相关性等级、DCG、IDCG、NDCG、MRR 与首个相关位置，`recompute_metrics` 可完全独立重算。
- 精确路径：全词典精确证据 + 词法候选 + 点击两段式消歧。
- 近似路径：字符 bigram 受限候选、按 token 截断、意图候选剪枝与模糊回退；与精确路径共享归一化、偏移、槽位裁决和置信口径。
- 规模取舍：按查询数、词典条目数、平均 token 数、内存余量、比较预算选择 `exact` 或 `pruned_approx`，决策记录包含观测、代价、原因和 SHA-256 校验和。
- 预算准入：比较数按 `query_count * candidate_count * ceil(avg_tokens)` 估算，内存按词典基线、每查询 token 与输出开销估算；优先级固定为 `precision > memory > progress`。
- 版本快照：taxonomy、dictionary、config 都带单调递增版本和内容校验和；快照整体替换，不增量合并。回退请求返回 `E_SNAPSHOT_ROLLBACK`，校验失败返回 `E_SNAPSHOT_CORRUPT`。
- 异常注入：`normalize`、`tokenize`、`slot`、`intent` 四阶段可由参数或输入字段触发；每次产生阶段名、业务码、降级类别；自检失败返回 `E_INCONSISTENT` 并给出首类不一致。

更完整的设计、公式和失败策略见 `docs/DESIGN.md`。

## 当前验证结果

| 验收项 | 结果 |
| --- | --- |
| 小规模路径 | `exact` |
| 大规模路径 | `pruned_approx` |
| 两路径意图一致率 | `1.0` |
| 槽位偏移差异 | `0` |
| 最大置信偏差 | `0.0` |
| 小规模 NDCG@3 / MRR | `0.917021` / `1.0` |
| 大规模 NDCG@3 / MRR | `0.917021` / `1.0` |
| 小规模覆盖 | 96/96 |
| 大规模覆盖 | 576/576 |
| 预算裁决 | accepted / downbatch / `E_MATCH_BUDGET` / `E_RESOURCE_LIMIT` 均验证 |
| 快照 | v1→v2 整体替换、回退拒绝、损坏拒绝均验证 |
| 四阶段注入 | 四种降级均通过自检 |
| 压力场景 | 剪枝遗漏与消歧口径差异均返回 `E_PATH_INCOMPARABLE` |

运行 `python -m query_understanding run` 会重新生成上述报告，并校验所有数据 SHA-256、指标可重算性、结果重复运行的逐字节一致性，以及两条路径的覆盖完整性。

