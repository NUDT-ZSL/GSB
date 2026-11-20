# 本地中文知识图谱构建管线与验证工具

这是一个只依赖本地文件和 Python 标准库的确定性知识图谱构建管线。它把一次构建拆成：分片摄入与校验、中文分词与实体识别、关系模式匹配、实体归一与去重、图谱统计、跨路径裁决、版本化快照和独立结果验证。

项目不访问在线服务，不使用数据库、消息队列或容器。样本语料、实体词典、关系模式表、真值清单和抽取配置都由固定种子生成并随项目保存在 `data/`。

## 1. 环境

- Python 3.11 及以上。
- 运行时代码无第三方依赖；NumPy 不是必需项。本机实际验收环境为 Python 3.12.13、NumPy 2.5.5。
- 当前开发执行环境是 WSL/Linux，没有可用的 `pwsh`。PowerShell 命令与脚本已提供，实际自动化验收在 Bash 下执行。

PowerShell：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python --version
python -m kgpipe --help
```

如果 `py -3.11` 未注册，可先安装 Python 3.11+ 并勾选“Add Python to PATH”，或将 `python` 指向已安装的 3.11+ 解释器。

Bash：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python --version
python -m kgpipe --help
```

无需 `pip install` 即可从仓库根目录运行 `python -m kgpipe`；命令入口不依赖网络安装。

## 2. 快速开始

PowerShell：

```powershell
$env:PYTHONUTF8 = "1"

python -m kgpipe generate-data --out data --seed 20261121
python -m kgpipe run --data data --out artifacts/run --mode auto --pair-budget 12000
python -m kgpipe validate --data data --report artifacts/run/report.json
python -m kgpipe snapshot-verify --snapshot-dir artifacts/run/snapshots

python -m kgpipe demo --out artifacts/demo
Get-Content .\artifacts\demo\demo-summary.json -Raw | ConvertFrom-Json | Format-List
```

也可运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_demo.ps1 -Python python -Out artifacts/demo
```

Bash：

```bash
PYTHON=python3.12 ./scripts/run_demo.sh artifacts/demo
python -m unittest discover -s tests -v
```

## 3. 本地数据契约

### 3.1 语料分片

`data/shards/*.jsonl` 每行一个句子：

```json
{"shard_id":"shard-0001","doc_id":"doc-0001","sent_no":0,"text":"张三任职于国防科技大学。","char_len":12}
```

- `start`/`end` 使用 Python 字符串码点偏移，半开区间 `[start, end)`。
- `char_len` 必须等于 `len(text)`，不按 UTF-8 字节或显示宽度计算。
- 同一 `(doc_id, sent_no)` 只能出现一次；一个分片文件只能含一个 `shard_id`。
- 文档可跨分片聚合，但关系匹配只发生在同一句内，跨度带 `sent_no`。

### 3.2 实体词典

`data/dictionary.json` 包含：

- `dict_version`：单调递增版本。
- `checksum`：规范化 JSON 内容校验和。
- `entries`：规范实体 ID、规范名、类型、类型编号和别名。
- `normalization_map`：可选别名到目标实体的规范化映射。

一个别名字面量只能映射到一个规范实体。重复别名返回 `E_ALIAS_CONFLICT`。自引用或映射环返回 `E_NORMALIZE_CYCLE`，错误对象带 `alias`、`cycle_path`。

### 3.3 关系模式表

`data/relation_schema.json` 的每个模式有：

- `line_no`：模式行号。
- `relation_type`、`relation_type_id`：关系类型及稳定编号。
- `head_type`、`tail_type`：头尾实体类型约束。
- `trigger`、`max_gap`：触发词和最大实体间距。
- `mandatory`：是否强制必须在近似路径中保留。
- `score_components`：整数基点分项。

置信度必须能由明细重算：

```text
gap = tail.start - head.end
distance_bp = max(0, distance_bonus_bp - gap_penalty_bp * gap)
confidence_bp = type_bp + trigger_bp + distance_bp
confidence = confidence_bp / 10000
```

触发词必须完整位于头实体结束与尾实体开始之间。每个候选记录 `pattern_line`、`pattern_id`、`trigger`、`trigger_offset` 和全部分项。

### 3.4 真值清单

`data/truth.json` 包含 `truth_version`、`checksum` 和三元组真值。真值按 `(doc_id, head_entity_id, tail_entity_id, relation_type)` 去重评估，报告精确率、召回率和 F1。

### 3.5 抽取配置

`data/config.json` 可覆盖的重要字段：

- `pair_budget`：总比较预算，单位为“实体对 × 模式”比较单元。
- `memory_limit_mb`、`reserved_memory_mb`：驻留内存预算与预留量。
- `bytes_per_pair`：单个比较单元的确定性内存估算。
- `approx_neighbors`：近似路径每头实体最多查看的后续邻域实体数。
- `budget_policy`：`error` 或 `degrade`。
- `exact_entity_soft_limit`：实体密度观测阈值。

## 4. 管线阶段与裁决

### 4.1 实体识别

- 中文汉字按单码点、连续 ASCII 字母数字按词、标点单独成词，输出确定性分词结果。
- 词典匹配从左到右执行确定性最大匹配；同一起点先取最长跨度，同长同起点按 `type_id`、别名、实体 ID 升序打破平局。
- 选定跨度互不重叠，包括 `start`、`end`、表面字面量、实体 ID、类型和 `dictionary_version`。
- 同一输入、词典版本和配置下，跨度集合可逐字节复现。

### 4.2 候选三元组排序

候选去重后按以下顺序升序排列：

```text
(-confidence_bp, head_start, tail_start, relation_type_id,
 sent_no, head_end, tail_end, pattern_line, trigger_offset)
```

主裁决顺序满足“同分时按头跨度起点、尾跨度起点、关系类型编号”。

### 4.3 精确与近似路径

设某句实体数为 `n`，模式数为 `p`，强制模式数为 `m`，近似邻域为 `k`：

```text
exact_pair_scans      = Σ n(n-1)/2
exact_comparisons     = exact_pair_scans * p + shard_count * 8
approx_pair_scans     = Σ min(k, n-1) 邻域对数
approx_comparisons    = 强制模式全量比较 + 非强制模式邻域比较 + 分片开销
estimated_memory      = comparisons * bytes_per_pair
```

决策输入完整写入 `selection.observations`，并生成 `decision_sha256`。同一观测和配置下决策记录逐字节一致。

- 精确预算与内存均满足：走 `exact`。
- 超预算或内存不足且策略为 `error`：返回 `E_PAIR_BUDGET` 或 `E_RESOURCE_LIMIT`。
- 策略为 `degrade`：走 `approx`，在 `selection.degradation` 记录类别、`affected_docs`、邻域上限和批量规模。
- 近似路径仍超限时自动降低 `approx_neighbors`；无法再降则返回资源或预算错误。
- `mandatory=true` 的模式在近似路径中始终使用精确枚举，不会被邻域裁剪遗漏。

### 4.4 跨路径一致性

`--mode both` 同时输出精确与近似结果，比较：

- `missing_in_approx`、`extra_in_approx`；
- 公共三元组的逐条置信度偏差与模式/触发词偏差；
- 逐文档与聚合召回估计；
- 差异来源：近似枚举遗漏或模式匹配口径不同。

只要存在遗漏、额外三元组或分数偏差，`statistics_comparable=false`，返回 `E_PATH_DIVERGENCE` 并记录首次分歧的 `doc_id` 和三元组。最终规范化图谱固定采用精确路径，避免混用两个口径。

### 4.5 归一化、去重与统计

- 每个跨度解析到一个规范实体；文档别名映射和规范实体别名集合必须与跨度字面量互相核对。
- 最终三元组按 `(doc_id, head_entity_id, tail_entity_id, relation_type)` 去重。
- 每条三元组只保留一条确定性最佳记录，并记录被抑制候选 ID。
- 图谱统计由逐文档规范化明细计算；验证器从明细重新计算并逐字段比较。

### 4.6 快照

快照目录包含 `manifest.json` 和 `config/dictionary/relation_schema/graph_stats` 四类快照。

- 每类快照有单调递增版本、payload 校验和、文件 SHA-256。
- 新版本原子写入并整体替换，旧快照文件删除；同版本内容变化拒绝增量修补。
- 缺失、JSON 损坏、payload 校验和不符、文件校验和不符均返回 `E_SNAPSHOT_CORRUPT`，并指出 `snapshot_category` 和 `version`。

### 4.7 独立验证

`validate` 命令和管线内自检从源数据重新加载分片、词典、模式表和真值：

- 重算每个文档的跨度、分词、候选三元组；
- 校验跨度边界、表面字面量和不重叠；
- 校验三元组端点存在、同句、置信分项重算、模式行和触发词偏移；
- 校验别名唯一性、跨度与别名互证、最终三元组唯一；
- 由逐文档明细重算图谱统计；
- 校验近似路径未遗漏强制模式；
- 任何不一致都返回 `E_INCONSISTENT`，并带 `category` 与可用的 `doc_id`。

## 5. 命令与退出码

| 命令 | 作用 |
| --- | --- |
| `generate-data` | 生成固定种子样本 |
| `run` | 执行一次构建 |
| `validate` | 独立复核报告 |
| `snapshot-verify` | 校验快照清单和内容 |
| `demo` | 运行完整验收场景 |

| 退出码 | 含义 |
| --- | --- |
| `0` | `ok`、`degraded` 或验证成功 |
| `3` | `run` 返回硬错误 |
| `4` | 结果验证失败 |
| `5` | 快照校验失败 |

主要错误码：

| 错误码 | 含义 |
| --- | --- |
| `E_INGEST` | 分片字段、UTF-8、行号、`char_len` 或重复句异常 |
| `E_ALIAS_CONFLICT` | 别名冲突 |
| `E_NORMALIZE_CYCLE` | 规范化自引用或环 |
| `E_PAIR_BUDGET` | 比较预算超限 |
| `E_RESOURCE_LIMIT` | 驻留内存估算超限 |
| `E_PATH_DIVERGENCE` | 精确/近似路径统计口径不可比 |
| `E_SNAPSHOT_CORRUPT` | 快照损坏或校验和不符 |
| `E_SNAPSHOT_MISSING` | 期望的快照类别缺失 |
| `E_SNAPSHOT_VERSION` | 快照版本非单调递增 |
| `E_DICTIONARY_INVALID` / `E_SCHEMA_INVALID` / `E_TRUTH_INVALID` / `E_CONFIG_INVALID` | 输入结构或版本字段无效 |
| `E_INCONSISTENT` | 结果验证或跨阶段一致性失败 |

## 6. 输出

`run` 的主要输出：

```text
artifacts/run/
  report.json
  pipeline.log
  snapshots/
    manifest.json
    config-v000001.json
    dictionary-v000001.json
    relation_schema-v000001.json
    graph_stats-v000001.json
```

`report.json` 的关键区域：

- `selection`：规模观测、代价估计、路径选择、降级信息和决策哈希。
- `stages.entity_recognition`：逐文档跨度、分词和词典版本。
- `stages.relation_extraction.paths`：按路径的候选三元组。
- `stages.normalization.documents`：规范实体、别名映射、去重后三元组。
- `stages.graph.statistics`：可由逐文档明细重算的图谱统计。
- `cross_path`：双路径差异、置信偏差和召回估计。
- `evaluation`：对真值清单的精确率、召回率和 F1。
- `self_check`：独立验证结果。
- `errors`/`warnings`：硬错误和降级告警。

## 7. 实际验收结果

本次在 Python 3.12.13 下执行，样本参数为 `seed=20261121`：

- 样本规模：4 个分片、10 个文档、24 句、57 个实体跨度、33 条规范化三元组。
- 固定种子除记录在元数据中，还确定性轮转文档的分片归属；相同种子逐字节复现，不同种子只改变分片布局。
- 基线：`exact` 路径，真值 F1=`1.0`，自检 `valid=true`。
- 双路径高邻域：精确与近似完全一致，聚合召回 `1.0`，`statistics_comparable=true`。
- 双路径低邻域：`E_PATH_DIVERGENCE`，首次分歧 `doc-0002`，聚合召回 `0.727273`，差异来源为近似枚举遗漏。
- 预算降级：精确估算 384 个比较单元，预算 300；降级到近似路径，近似估算 276，`E_PAIR_BUDGET`，受影响文档 `["doc-0002"]`，结果 `status=degraded`。
- 异常分片：破坏 `char_len` 后返回 `E_INGEST`。
- 快照损坏：修改 graph payload 后快照校验返回 `E_SNAPSHOT_CORRUPT`。
- 快照替换：graph 快照 v1 创建、v2 `whole_replace`，加载版本为 2，旧文件删除。
- 单元测试：18 项全部通过，覆盖确定性生成、跨度复现、别名冲突、规范化环、预算/内存、降邻域、跨路径分歧、强制模式保留、快照损坏/整体替换、报告篡改和统计重算。

完整结果见 `artifacts/demo/demo-summary.json`、`artifacts/verification/acceptance.txt`、`artifacts/verification/test-results.txt` 及各子目录 `report.json`。

## 8. 项目结构

```text
kgpipe/
  corpus.py        # 分片摄入与校验
  dictionary.py    # 词典、别名和规范化异常
  tokenizer.py     # 确定性分词
  recognition.py   # 最大匹配实体识别
  relation.py      # 模式、打分、精确/近似枚举
  adaptive.py      # 预算、内存和降级裁决
  paths.py         # 跨路径比较
  normalization.py # 归一化、去重和图谱统计
  snapshot.py      # 版本化快照
  verifier.py      # 独立复核和真值评估
  pipeline.py      # 阶段编排
  cli.py           # 命令行
  demo.py          # 验收演示
tests/test_core.py
scripts/run_demo.ps1
scripts/run_demo.sh
```

## 9. 边界

- 当前分词器面向混合中文/ASCII 的确定性样本，不提供统计分词模型。
- 关系匹配限定同句、头实体在尾实体之前，触发词位于两者之间。
- 自适应内存为配置驱动的确定性估算，不读取宿主机的瞬时空闲内存，以避免同一观测产生不同决策。
- 本工具只处理本地授权的确定性样本，不发起网络请求。

