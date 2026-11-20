# qxworkbench

本地、确定性、无外部服务的跨语言查询改写与索引调度工作台。它把以下四个环节放在同一条可审计链路中：

1. 查询改写规则 v1/v2/v3 的解析与无损/有损迁移；
2. 跨语言词项桥接的版本化、多跳展开与排序；
3. 按语言分区的共享倒排索引与 BM25 top-k 检索；
4. 规则、桥接、索引三类快照的协同版本化、强制终止恢复与一致性自检。

项目只使用 Python 标准库和 NumPy，不访问外部服务，不使用容器。所有样本、快照、提交记录和验证产物都在工作区本地生成。

## 环境

已在本工作区验证：

- Python `3.11.15`
- NumPy `2.4.6`
- Linux 本地进程池（32 CPU 可用）

从零复现环境：

```bash
uv venv --python 3.11 --seed .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -c "import numpy; print(numpy.__version__)"
```

如果系统同时有多个 Python，后续命令统一使用 `.venv/bin/python`。

## 快速运行

生成固定种子样本并跑完整演示：

```bash
.venv/bin/python -m qxworkbench demo --root artifacts/demo --workers 2
```

该命令会：

- 生成 20 篇中文、英文、法文、西班牙文文档；
- 生成 12 条查询；
- 生成规则 v1/v2/v3 与桥接资源 v1/v2/v3；
- 依次提交桥接 v1 / 索引 v1、桥接 v2 / 索引 v2、桥接 v3 / 索引 v3；
- 执行全部查询；
- 输出 `artifacts/demo/demo_report.json` 并通过自检。

完整验收：

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/verify.py
```

`scripts/verify.py` 会生成 `artifacts/verification/verification_report.json`、测试日志、演示快照、故障注入后的恢复快照和五类错误码探针结果。

## 数据与规则格式

样本由 `qxworkbench.samplegen` 用固定种子生成：

- `documents.jsonl`：`doc_id`、`doc_lang`、`terms`（`term`/`freq` 数组）、`doc_len`
- `queries.jsonl`：`query_id`、`query_lang`、`terms`、`top_k`
- `rules_v1.rules`：版本头 + 五字段无字段名行式规则
- `rules_v2.rules`：版本头 + `name=value` 行式规则
- `rules_v3.json`：结构化 JSON，支持多目标语言和嵌套条件
- `bridge_v1.json` / `bridge_v2.json` / `bridge_v3.json`：语言对方向、词项桥接、置信度、版本与内容校验和

### v1

```text
# version=v1
r_car_zh_en|zh|汽车|en|car
```

固定五列：`rule_id|source_lang|source_term|target_lang|target_term`。字段数不符、缺失、空值都会返回 `E_FORMAT_CORRUPT`，并携带 `source` 与 `line`。

### v2

```text
# version=v2
rule_id=r_car_zh_en|source_lang=zh|source_term=汽车|target_lang=en|target_term=car|bridge_confidence_weight=0.95|max_expansion_depth=2|enabled=true|condition_terms=汽车
```

五个核心字段必填；`bridge_confidence_weight`、`max_expansion_depth`、`enabled`、`condition_terms` 可选。数值不可解析、重复字段、未知字段、要求字段缺失均返回 `E_FORMAT_CORRUPT`。

### v3

```json
{
  "format_version": "v3",
  "rules": [
    {
      "rule_id": "r_car_zh",
      "source_lang": "zh",
      "source_term": "汽车",
      "targets": [
        {"lang": "en", "term": "car"},
        {"lang": "fr", "term": "voiture"}
      ],
      "condition": {
        "op": "all",
        "children": [
          {"op": "term_present", "term": "汽车"},
          {"op": "min_freq", "term": "汽车", "value": 1}
        ]
      },
      "bridge_confidence_weight": 0.95,
      "max_expansion_depth": 2,
      "enabled": true
    }
  ]
}
```

v3 可表达多目标语言和 `all`、`any`、`term_present`、`min_freq` 嵌套条件。也接受 `{"all": [...]}`、`{"any": [...]}`、`{"term_present": "..."}`、`{"min_freq": {"term": "...", "value": 2}}` 简写。

版本分派顺序：

1. 优先读取 `# version=...` 版本头；
2. 无版本头但内容为 JSON 对象/数组时走 v3；
3. 非 JSON 行全部包含 `=` 时走 v2；
4. 否则走 v1。

未知版本返回 `E_VERSION_UNSUPPORTED`。三种格式均用稳定 JSON 或固定字段顺序写出；同一输入、同一配置下产物逐字节一致。

## 迁移

```bash
.venv/bin/python -m qxworkbench migrate \
  --input samples/rules_v1.rules \
  --to v2 \
  --output artifacts/rules_v2.migrated.rules \
  --report-output artifacts/rules_v2.migration.json
```

支持任意方向，核心流程是 v1 ↔ v2 ↔ v3。

升级：

- 缺失的 `bridge_confidence_weight` 补 `1.0`；
- 缺失的 `max_expansion_depth` 补 `1`；
- 缺失的 `enabled` 补 `true`；
- 每个补全项在报告 `completions` 中记录 `rule_id`、`field`、`value`、`source`；
- v2 的 `condition_terms` 升级为 v3 的合取条件。

降级：

- v3 多目标默认 `flatten`：每个目标语言展开成一条 v2/v1 规则，并写入 `flattened`；
- `--multi-target-policy drop`：保留首个目标，其余目标写入 `drops`；
- v3 条件若可化为“多个 `term_present` 的 AND”，v2 展平为 `condition_terms`；
- `any`、`min_freq` 等 v2/v1 不可表达条件按策略写入 `drops`，不静默改变语义；
- v3/v2 降级到 v1 时，可选字段与条件写入 `drops`。

迁移器写出后会立即回读；测试覆盖 v1→v2→v1 与 v2→v3→v2 在可表达字段上的字节一致往返。

## 展开与桥接

`expand_query` 的确定性规则：

1. 只处理 `enabled=true` 且 `source_lang` 等于查询语言的规则；
2. 规则 `source_term` 必须出现在查询中，条件必须成立；
3. 从源词项沿桥接边做有界 BFS，边置信度相乘，路径长度不超过 `max_expansion_depth`；
4. 同一目标 `(target_lang, target_term)` 保留最高桥接置信度路径；
5. 候选有效置信度 = 路径桥接置信度 × 规则 `bridge_confidence_weight`；
6. 过滤低于 `min_confidence` 的候选；
7. 按 `有效置信度降序、目标词项字典序升序、目标语言、源词项、rule_id` 排序；
8. 截断到 `max_candidates`。

每个候选包含 `bridge_confidence`、`rule_weight`、`confidence`、`depth`、完整桥接 `path` 和 `rule_ids`。同一输入、规则版本和桥接版本下 `expansion_bytes()` 逐字节一致。

## 索引与打分

倒排索引按 `doc_lang` 分区。每个分区保存：

- `docs`：按 `doc_id` 升序；
- `postings`：按 `(doc_id, term)` 升序，含 `tf`、`doc_len`、源文件与行号；
- `term_index`：词项到 posting 下标的映射；
- `stats`：文档数、posting 数、平均文档长度；
- 规则/桥接/索引版本与校验和。

桥接差异只标记目标语言受影响的文档分区。例如样本中桥接 v1→v2 只改变 `en`、`fr` 目标词项，因此受影响分区是 `["en", "fr"]`，`es` 和 `zh` 不标记；v2→v3 只改变 `fr`，因此只有 `fr` 受影响。

检索使用 BM25：

```text
idf = log(1 + (N - df + 0.5) / (df + 0.5))
bm25 = idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * doc_len / avgdl))
contribution = bm25 * confidence * source_query_freq
```

默认 `k1=1.2`、`b=0.75`。同一文档命中多个候选时：

- `--merge-policy sum`：对贡献明细求和；
- `--merge-policy max`：取最大贡献。

每条贡献明细均包含词项、桥接路径、置信度、`tf`、`df`、`idf`、`bm25`、`contribution` 和来源 posting。最终分数只由输出明细按显式合并策略重算，`recomputed_score` 可用于自检。

查询同时输出：

- `baseline_results`：未改写、仅查询语言分区的直接 BM25；
- `rewritten_results`：跨语言候选贡献合并后的 top-k；
- `delta_spectrum`：两侧 top-k 文档并集上的 `baseline_score`、`rewritten_score`、`delta` 与排名。

## 快照与原子切换

提交根目录结构：

```text
state/
  rules/vN/manifest.json
  bridge/vN/manifest.json
  index/vN/manifest.json
  index/vN/partitions/<lang>.json
  commits/<commit-id>.json
  CURRENT
  builds/<build-id>/
```

规则快照、桥接快照、索引快照和提交记录都带内容校验和；索引还记录规则与桥接的版本和校验和。提交前交叉核对：

- commit 中的规则、桥接、索引版本与校验和；
- 索引 manifest 中的规则/桥接版本与校验和；
- 所有分区是否已 retag 到目标桥接版本；
- 复用与重算分区集合是否相交，并集是否覆盖受影响集合。

每次提交都创建完整的新索引快照目录，不复用旧目录做增量修改。`CURRENT` 先写临时文件再 `os.replace`，因此查询要么读到完整旧提交，要么读到完整新提交，不会混用新旧桥接。

旧提交目录不可变，仍可通过 `store.load_commit(commit_id)` 读取。版本未提升但内容变化、请求版本与活动版本不符、校验和篡改均返回 `E_VERSION_SKEW`。

## 分区级恢复

`--crash-after N` 会在当前构建第 N 个新分区写入并原子更新 build state 后直接终止进程：

```bash
.venv/bin/python -m qxworkbench build ... --crash-after 1
```

退出码为 `97`。恢复时同一命令去掉 `--crash-after` 再执行：

- 读取 build state 中已写入的分区校验和；
- 校验失败的未提交分区从缺失处重建；
- 已记录且校验通过的分区不重算；
- 若桥接版本已变化，先比较中断时桥接与当前桥接，只移除受影响分区；
- 未被新版本涉及的分区继续复用；
- 最终发布完整的新索引快照。

验证场景：桥接 v1 已提交；桥接 v2 构建在 `en` 分区完成后被强制终止；恢复时改为桥接 v3。`v2→v3` 只影响 `fr`，所以恢复复用 `en`、`es`、`zh`，只重算 `fr`。恢复后的索引校验和、12 条查询结果与不间断 v3 完整运行逐字节一致。

## 资源预算与降级

可配置预算：

- `--max-postings`：构建和查询阶段的 posting 总数上限，超限返回 `E_RESOURCE_LIMIT`；
- `--max-resident-bytes`：查询解码驻留上限，超限自动切换流式分区打分并标记 `DEGRADED_STREAMING_PARTITION_SCORING`；
- `--max-parallel-partitions`：并行重算分区数上限；
- `--parallel-policy degrade`：限制 worker 数并标记 `DEGRADED_LIMITED_PARALLELISM`；
- `--parallel-policy error`：超限直接返回 `E_RESOURCE_LIMIT`。

流式与驻留两种查询路径输出相同的分数、贡献明细和差谱，只有 `resource_mode` 与 `degraded` 标记不同。

## 自检与诊断

```bash
.venv/bin/python -m qxworkbench selfcheck \
  --store artifacts/demo/state \
  --queries artifacts/demo/samples/queries.jsonl
```

自检类别：

- `VERSION_CROSSCHECK`：commit、规则、桥接、索引版本/校验和交叉核对；
- `RULE_EXPANSION_BRIDGE_VERSION`：展开使用的桥接版本等于提交记录中的桥接版本；
- `BRIDGE_VERSION`：每个分区记录的桥接版本/校验和等于 commit；
- `CONTRIBUTION_RECOMPUTE`：每条结果可由贡献明细分值完全重算；
- `DELTA_SPECTRUM`：差谱可由基线/改写分数重算；
- `PARTITION_RECOVERY`：复用与重算集合无交集、并集覆盖受影响集合；
- `POSTING_PROVENANCE`：每条 posting 可反查源 JSONL 行及 `doc_id`；
- `INDEX_ORDER`、`INDEX_STATISTICS`、`INDEX_TABLE`：顺序、统计和倒排表一致性。

发现不一致返回 `E_INCONSISTENT`，`details.categories` 给出类别。

交互诊断：

```bash
.venv/bin/python -m qxworkbench diagnose \
  --store artifacts/demo/state \
  --queries artifacts/demo/samples/queries.jsonl
```

支持 `help`、`status`、`versions`、`queries`、`expand <query_id>`、`search <query_id>`、`selfcheck`、`quit`。所有非交互命令输出稳定 JSON。

## 错误码

| 错误码 | 含义 |
| --- | --- |
| `E_VERSION_UNSUPPORTED` | 未知规则、版本头或桥接格式 |
| `E_FORMAT_CORRUPT` | 字段数、必填字段、JSON、数值或校验和损坏，带源行定位 |
| `E_RESOURCE_LIMIT` | posting/内存/并行预算拒绝 |
| `E_VERSION_SKEW` | 三快照版本或校验和不匹配、请求版本不是活动版本 |
| `E_INCONSISTENT` | 跨模块自检失败，带类别明细 |

## 测试与验收

`tests/` 覆盖：

- 三格式确定性解析、未知版本、格式损坏和源行；
- 迁移默认补全、丢弃/展平明细、可回读和可表达字段往返；
- 桥接多跳、置信度过滤、最大候选与字典序排序；
- 分区顺序、posting 来源、受影响分区识别；
- 基线/改写/差谱、贡献重算、流式与驻留一致；
- 原子切换、旧提交可读、版本冲突；
- 强制终止、桥接版本变化后的分区级恢复；
- 资源拒斥/降级和自检异常。

最近一次完整验证见 `docs/VERIFICATION.md` 和 `artifacts/verification/verification_report.json`。

## 设计边界

- 桥接候选只来自显式桥接表；规则中的 `target.term` 是语义提示，不作为无桥接置信度的回退候选。
- 同一 `(target_lang, target_term)` 的多个等价桥接路径保留最高置信度，避免同词重复计分。
- v1/v2 是行式格式，字段值不能含竖线；v2 值不能含 `=`。结构化 JSON v3 无此限制。
- 验证使用进程级强制终止和本地文件系统，不覆盖断电级 `fsync`/文件系统损坏测试。
