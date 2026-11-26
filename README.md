# 搜索推荐融合诊断版

这是一个可在本地 Linux/WSL Ubuntu 直接运行的确定性四路召回融合诊断工程。工程使用 Python 3.10+、标准库 `sqlite3` 和 NumPy，不依赖网络、容器或外部服务。

默认样本由固定种子 `20261127` 生成，包含 32 篇文档、12 个查询、v1 单文件配置、v2 分片配置加 SQLite 元数据，以及 TF 修订、召回权重调整、归一化口径调整三类变体。

## 快速运行

```bash
python3 -m pip install -r requirements.txt
python3 scripts/generate_samples.py --out samples
python3 -m fusion_diag validate \
  --docs samples/docs.jsonl --queries samples/queries.jsonl \
  --vocab samples/vocab.txt --config samples/config_v2
python3 -m fusion_diag batch \
  --docs samples/docs.jsonl --queries samples/queries.jsonl \
  --vocab samples/vocab.txt --config samples/config_v2 \
  --workdir runs/demo --out runs/demo/batch.jsonl \
  --report runs/demo/report.json --verify-full
python3 -m fusion_diag query \
  --docs samples/docs.jsonl --queries samples/queries.jsonl \
  --vocab samples/vocab.txt --config samples/config_v2 \
  --workdir runs/demo --query-id q0006 --repeat 20 \
  --compare-batch runs/demo/batch.jsonl
```

交互查询默认输出包含 `result`、耗时样本和估算模型的诊断信封；`result` 是批量结果的原始对象。增加 `--raw` 可直接输出与批量 JSONL 中该查询完全相同的字节。

完整测试与验收：

```bash
python3 -m unittest discover -v
python3 scripts/run_acceptance.py
```

验收结果保存在 `artifacts/acceptance.json` 和 `artifacts/acceptance_report.md`。

## 输入与配置

### 文档 JSONL

每行一个对象：

```json
{"doc_id":"d0001","terms":[{"term":"alpha","tf":2}],"doc_len":9,"category":"books","quality_score":0.35}
```

摄入时校验 `doc_id` 唯一、词项在受控词表、`tf >= 1`、`doc_len` 为正整数、`quality_score` 在 `[0,1]`。文档按 `doc_id` 升序进入内存，再按 `(category, -quality_score, doc_id)` 预排序。预排序索引与统计量通过 canonical JSON 逐字节稳定输出。

### 查询 JSONL

```json
{"query_id":"q0001","terms":["alpha","beta"],"top_k":5,"filters":{"category":"books","min_quality":0.5}}
```

查询按 `query_id` 升序处理。`filters` 可选，支持 `category` 和 `min_quality`。格式错误统一返回 `E_FORMAT_CORRUPT`，并带 `line` 与 `field`。

### 配置快照

四路召回固定为 `lexical`、`prefix`、`category`、`vector`，每路声明启用状态、候选上限和融合权重。融合层声明归一化、去重口径、top-k 比较策略、容差、总候选预算、并行 worker 数和驻留内存上限。

v1 是单文件 JSON，记录相对生效秒：

```text
samples/config_v1.json
```

v2 是目录快照：

```text
samples/config_v2/
  manifest.json
  shards/recall_lexical.json
  shards/recall_prefix.json
  shards/recall_category.json
  shards/recall_vector.json
  shards/fusion.json
  metadata.sqlite
```

`metadata.sqlite` 保存绝对毫秒、配置代数、摘要、分片摘要、迁移来源日志和失效墓碑。

双向迁移：

```bash
python3 -m fusion_diag migrate --source samples/config_v1.json \
  --destination runs/migrated_v2 --source-schema v1 --destination-schema v2
python3 -m fusion_diag migrate --source runs/migrated_v2 \
  --destination runs/migrated_v1.json --source-schema v2 --destination-schema v1
```

v1→v2 会补全 `effective_at_ms`、`generation`、`digest` 和迁移墓碑，并逐项记录来源。v2→v1 对 v1 无法表达的墓碑使用显式规则 `not_representable_in_v1_explicit_drop` 丢弃并报告。

## 确定性算法

### 四路召回

- `lexical`：按查询词项累加 BM25 类分数，使用语料文档数、文档频率、`doc_len`、平均文档长度以及配置的 `k1/b`。
- `prefix`：查询词项与文档词项的最长公共前缀达到 `min_prefix` 后，按前缀占比和 `log1p(tf)` 赋分，每个查询词项取文档内最佳匹配。
- `category`：按类别平均质量和文档 `quality_score` 赋分，应用查询过滤后截取。
- `vector`：用配置种子和 `doc_id` 生成固定文档向量，用种子、`query_id` 和查询词项生成查询向量，计算余弦相似度。

每路候选按 `(-score, doc_id)` 排序，并受该路 `limit` 约束。浮点结果在 `1e-12` 位归一化，次正规数归零。

### 对齐、去重与融合

所有路候选按 `doc_id` 归并。每个候选逐路保存：

```text
route, raw_score, normalized_score, weight, contribution
```

`minmax` 在每路候选内缩放；`reciprocal_rank` 使用 `1/rank`。最终分数是全部贡献的 `math.fsum`，并可只依赖明细独立重算。

去重策略支持：

- `highest_fusion`：保留加权贡献最高的来源作为代表。
- `first_hit`：按固定路顺序保留首个来源。

代表性来源之外的完整路命中保存在 `dedup.discarded_route_hits`。归并后仍出现重复或来源缺失时返回 `E_CANDIDATE_DROP` 并带 `doc_id`。总候选预算在融合排序后截取，超出部分保存在 `trace.budget_dropped`。

top-k 支持严格排序和容差带排序。容差带内按 `doc_id` 升序，避免浮点近邻导致不稳定排名。

### 内容寻址缓存

每路缓存键包含：

```text
route 配置参数（不含融合权重）
查询内容哈希
该路依赖文档内容哈希
```

融合缓存键包含：

```text
documents_content_hash
query_content_hash
config_parameter_version
upstream_node_hash
```

`documents_content_hash` 是查询依赖闭包中的文档内容摘要：词法路包含语料统计和命中词项文档，前缀路包含前缀命中文档，类别路包含过滤后类别/质量信息，向量路包含过滤后 `doc_id`。这样既能保证语义正确，又能让无关查询保持复用。

缓存快照使用 canonical JSON 原子写入，并带自身的 payload SHA-256。键相同直接读取，键不同则重新计算。

## 增量失效与一致性

`batch --mode auto` 会比较当前查询计划与已提交记录：

- 查询内容、查询词项依赖或任一路配置发生变化：重算该查询。
- 仅某路融合权重变化且该查询未使用该路：复用已提交融合结果。
- 仅归一化口径变化：所有查询重算。
- 文档 TF 变化且 `doc_len`/全局统计不变：只有依赖该批词项的查询重算。

未受影响的查询直接复用已提交结果；受影响查询可以复用未变化的路缓存，只重建缺失的召回、对齐与融合后缀。报告输出：

```text
recomputed_query_ids
reused_query_ids
recompute_reasons
reuse_reasons
diff_coverage
```

`--verify-full` 会在临时目录执行语义全量重算，并逐查询比较 canonical JSON 字节。

## 检查点、恢复与资源控制

批量执行在五个边界写入检查点：

```text
ingest -> recall -> align -> fusion -> commit
```

检查点保存在 `runs/<name>/state/state.sqlite`，包含单调递增版本号、`run_key`、阶段、已完成 `query_id` 集合、内容校验和及阶段输出。恢复时：

1. 从最近检查点继续下一阶段。
2. 已提交查询不重算、不重复写入 `commit_audit`。
3. 未形成阶段检查点的部分状态丢弃，从该阶段起点重建。

可用 `--crash-after fusion` 模拟强制终止；CLI 返回码为 `137`。再次执行同一命令会从 `fusion` 检查点继续提交。

批量与交互入口共用 `runs/<name>/stage.lock` 非阻塞独占锁，后到者立即返回 `E_STAGE_BUSY`。每个阶段开始前检查 `/proc/self/status` 的 RSS 加阶段 payload 估算值，超限返回 `E_RESOURCE_LIMIT`。

## 交互查询与延迟

```bash
python3 -m fusion_diag query ... --query-id q0006 --repeat 20
```

输出包括四路召回、对齐去重轨迹、每路贡献、融合分数、top-k、当前配置版本、实际耗时样本、候选数、归一化计算次数、融合权重数量和延迟估算。`--raw` 用于与批量记录做逐字节验收。

## 实测验收结果

环境：Python `3.10.12`、NumPy `2.0.0`、SQLite `3.37.2`。运行 `python3 scripts/run_acceptance.py` 的 13 项检查全部通过：

- 固定样本摄入：32 篇文档、12 个查询。
- 基础全量输出 SHA-256：`64f4b951203d73defc7a6e898f086949cd2b7f1239500ef2613d80648f239a27`。
- 文档 TF 修订：仅重算 `q0001/q0006`，复用 10 个查询，全量差异 12/12 字节一致。
- 前缀路权重调整：重算 10 个查询，复用 `q0008/q0011`，全量差异一致。
- 归一化改为排名倒数：12 个查询全部重算，全量差异一致。
- 交互 `q0006` 与批量对应记录逐字节一致，核心 SHA-256 为 `cbeac1e011eee84a2f7e2772d9fb8ed525ee6edf6a19c898af63628a19f5002d`。
- 本地 20 次查询循环本次实测均值 `1.25 ms`、P50 `0.67 ms`、P95 `4.72 ms`（计时受机器负载影响）。
- `fusion` 阶段强制终止后从检查点恢复；`q0006` 提交审计计数保持 1。
- 并发冲突返回 `E_STAGE_BUSY`，1 MiB 内存上限返回 `E_RESOURCE_LIMIT`，损坏质量分返回 `E_FORMAT_CORRUPT` 且 `line=1, field=quality_score`。

详细数据见 `artifacts/acceptance.json`，可读报告见 `artifacts/acceptance_report.md`。

## 代码结构

```text
fusion_diag/ingest.py      JSONL 校验、预排序、统计
fusion_diag/recall.py      四路打分、依赖指纹、路缓存键
fusion_diag/fusion.py      归一化、对齐、去重轨迹、top-k
fusion_diag/config.py      v1/v2 快照、摘要、互迁
fusion_diag/cache.py       内容寻址快照
fusion_diag/state.py       提交表、检查点、阶段锁
fusion_diag/engine.py      全量/增量流水线、恢复、资源检查
fusion_diag/cli.py         统一批量与交互入口
fusion_diag/generate.py    固定种子样本生成
tests/                     26 个自动化测试
scripts/run_acceptance.py  端到端验收与报告生成
```
