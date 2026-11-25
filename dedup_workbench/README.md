# LLM Pretraining Corpus Dedup Diagnostic Workbench

一个完全本地、确定性的语料去重诊断工作台。它把“摄入校验 → 分块签名 → 哈希桶/候选窗口 → 偏移比对/阈值裁决 → 聚合导出”实现为带内容寻址缓存、增量失效、查询一致性、快照版本、并发锁、资源上限和检查点恢复的多级计算图。

不依赖 Docker、外部网络或数据集。固定种子样本由 `scripts/generate_sample.py` 生成。

## 快速运行

```bash
cd dedup_workbench
python3 scripts/generate_sample.py
PYTHONPATH=src python3 -m dedup_workbench.cli run \
  --input samples/corpus_shards.jsonl \
  --config samples/dedup_config.json \
  --workdir reports/manual_runtime \
  --report reports/manual_run.json

PYTHONPATH=src python3 -m dedup_workbench.cli query \
  --workdir reports/manual_runtime \
  --config samples/dedup_config.json \
  --doc-id doc-0002

PYTHONPATH=src pytest -q
python3 scripts/verify_workbench.py
```

验收脚本会重新生成样本、运行全部 pytest、执行五类增量变更、查询逐字节比对、延迟预算探测和 SIGKILL/检查点恢复，并写出：

- `reports/acceptance_report.json`：结构化验收报告。
- `reports/ACCEPTANCE_REPORT.md`：可阅读摘要。

## 目录结构

```text
scripts/generate_sample.py     # 固定种子 JSONL + 配置生成器
scripts/verify_workbench.py     # 端到端验收和报告生成
src/dedup_workbench/
  ingest.py                    # JSONL 排序、归一化、哈希和字段校验
  signatures.py                # 分块签名、文档摘要、桶摘要
  candidates.py                # 哈希桶、候选窗口、候选闭包
  compare.py                   # 块偏移、摘要冲突、精确相似度
  decide.py                    # 阈值、DSU 簇、质量分代表筛选
  cache.py                     # CAS + 内存上限/优先级驱逐
  snapshots.py                 # 单调版本、校验和、E_STAGE_BUSY
  checkpoint.py                # 批次检查点和强杀恢复
  query.py                     # doc_id 查询、计时和延迟预算
  selfcheck.py                 # E_INCONSISTENT 一致性自检
  engine.py                    # DAG 编排、增量分区和导出
tests/                         # 34 个确定性 pytest 用例
```

## 确定性规则

- 文档按 `(shard_id, doc_id)` 升序处理。
- 文本归一化：`unicodedata.normalize("NFKC", text)` 后将连续空白折叠为单个空格并去除首尾空白。
- `content_hash = sha256(normalized_text.encode("utf-8"))`。
- 按 `chunk_signature_length` 个字符做无重叠切分；最后一块允许短于配置长度。
- 每个块输出 `position/start/end/length/signature`，块签名为块文本的 SHA-256。
- 文档摘要为有序块签名列表的 canonical SHA-256：JSON 使用 `sort_keys=True`、分隔符 `(",", ":")`、`ensure_ascii=False`。
- 默认 `anchor_digest` 桶策略取文档内最小的 `bucket_anchor_count=3` 个块摘要作为“文档级桶摘要”；同时保留：
  - `block_digest`：每个唯一块摘要一个精确桶，召回最强。
  - `doc_digest`：按完整文档摘要前缀分桶，适合精确重复。
- 桶内按 `(shard_id, doc_id)` 排名，仅当排名距离不超过 `candidate_window` 时生成候选对；每条候选都记录所有来源桶及排名距离。

## 相似度与代表

比较节点构造块摘要到位置的有序映射，按多重集交集生成命中位置 `(position_a, position_b, offset_b_minus_a)`。

主相似度为：

```text
similarity = hit_block_count / max(block_count_a, block_count_b)
```

阈值比较使用 `fractions.Fraction` 和 `similarity_exact`，不做浮点容差或近似截断。另输出 Jaccard 和同位命中率作为诊断量。

裁决先对达到阈值的边做 DSU 聚类，再在每个近重复簇内按 `(-quality_score, doc_id)` 显式排序选出代表。被剔除文档记录：

- 代表和近重复邻居；
- 命中块位置与偏移；
- 实际相似度、阈值、精确分数差和 `passed`；
- 质量分排序明细。

## 查询与批量一致性

批量运行导出 canonical JSONL：`exports/doc_records.jsonl`。每行就是一个完整 `doc_id` 记录，包含块签名、候选比较轨迹、裁决和剔除状态。

`QueryService.query(doc_id)` 在本地循环中实际解析该行并用 `time.perf_counter_ns()` 计时，NumPy 计算均值、中位数和 P95；同时按 `signature_block_count` 与 `comparison_count` 给出估算延迟。查询返回的 `record` 重新 canonical 序列化后必须与批量导出行逐字节相同。实测平均延迟超过 `limits.latency_budget_ms` 或估算值超预算时返回 `E_LATENCY_BUDGET`。

## 增量失效矩阵

缓存键包含节点名、该节点全部关键输入摘要、上游产物哈希、节点配置版本和相关参数。全局快照版本用于快照提交；节点缓存键使用阶段配置版本和该阶段相关参数，避免无关配置变更使未受影响节点失效。

| 变更 | 签名 | 候选桶 | 比较 | 裁决 |
|---|---|---|---|---|
| 相同输入/配置重跑 | 复用 | 复用 | 复用 | 复用 |
| `similarity_threshold` | 复用 | 复用 | 复用 | 受影响候选分量重算 |
| `validation_mode` | 复用 | 复用 | 全部候选对重算 | 重算 |
| `candidate_window` / 桶参数 | 复用 | 重算 | 相同对复用、新对重算 | 按候选分量重算 |
| `chunk_signature_length` | 全部重算 | 重算 | 重算 | 重算 |
| 单个 `content_hash` 修订 | 仅变更文档签名失效 | 全局候选闭包重算 | 未变对复用、涉变对重算 | 受影响分量重算 |

每次运行输出：

- `recomputed_docs` / `reused_docs`；
- `signature_recomputed_docs`；
- `comparison_recomputed_pairs` / `comparison_reused_pairs`；
- `decision_recomputed_components` / `decision_reused_components`；
- `disjoint` 与 `covers_all_docs`。

自检强制两个文档集合无交集且并集等于当前全部文档。

## 缓存、快照、并发与恢复

- CAS 路径：`cas/objects/<key-prefix>/<cache_key>.json`；信封记录 `cache_key`、`payload_checksum` 和完整键输入。
- 内存缓存有 `limits.max_cache_memory_bytes` 上限；默认 `evict_then_spill` 按 `(priority, cache_key)` 显式驱逐低优先级条目并保留磁盘产物；设置 `cache_overflow_policy=error` 时超限返回 `E_RESOURCE_LIMIT`。
- `snapshots/config_snapshot.json`、`stats_snapshot.json`、`removals_snapshot.json` 均携带单调递增 `version` 与绑定版本/数据的 SHA-256 `checksum`，通过临时文件 + `os.replace` 整体替换。
- 安装旧版本快照返回 `E_SNAPSHOT_ROLLBACK`；校验和损坏返回 `E_SNAPSHOT_CHECKSUM`。
- 配置提交使用进程内非阻塞锁和 `fcntl` 非阻塞文件锁；竞争提交立即得到 `E_STAGE_BUSY`，不等待。
- 分片按 `limits.batch_size` 分批；每批签名进入 CAS 后持久化 `checkpoints/batch_executor.json`。重启后跳过 `committed_batch_indexes`，只执行后续批次。测试包含提交后真实 `SIGKILL` 子进程恢复。

## 结构化业务码

| 业务码 | 触发条件 | 定位字段 |
|---|---|---|
| `E_JSONL_PARSE` | JSONL 语法错误或无输入文件 | `path:line` |
| `E_INPUT_RECORD` | 缺字段/类型错误 | 对应字段 |
| `E_DUPLICATE_DOC_ID` | 全局 doc_id 重复 | `doc_id` |
| `E_EMPTY_TEXT` | NFKC 归一化后为空 | `text` |
| `E_CONTENT_HASH_FORMAT` / `E_CONTENT_HASH_MISMATCH` | 哈希格式或文本一致性失败 | `content_hash` |
| `E_LANG_NOT_ALLOWED` | 语言不在受控集合 | `lang` |
| `E_CONFIG_INVALID` | 配置参数非法 | 具体配置字段路径 |
| `E_RESOURCE_LIMIT` | 分片数、块数或缓存内存超限 | `limits.*` / `doc_id` |
| `E_LATENCY_BUDGET` | 查询实测或估算延迟超预算 | `limits.latency_budget_ms` |
| `E_STAGE_BUSY` | 已有配置快照提交占用锁 | `commit_lock` |
| `E_SNAPSHOT_ROLLBACK` | 试图安装更旧/相等快照版本 | `<kind>.version` |
| `E_SNAPSHOT_CHECKSUM` | 快照内容校验和不一致 | `<kind>.checksum` |
| `E_INCONSISTENT` | 跨节点集合、闭包或清单不一致 | 自检失败项 |

## 实际验证结果

最近一次本地运行（Python 3.10.12、NumPy 1.26.4、pytest 8.2.0）：

- `PYTHONPATH=src pytest -q`：`34 passed`（当前环境的精简 reporter 仅显示 34 个点，退出码为 0）。
- `python3 scripts/verify_workbench.py`：`ok=true`。
- 固定样本：25 条文档、4 个 shard、192 个签名块、27 个候选对、27 个达到阈值的近重复对、15 个剔除文档。
- 25/25 个 `doc_id` 查询记录与批量 canonical 导出行逐字节一致。
- 相同输入/配置二次运行：0 个文档重算、25 个文档复用、27 个比较全部复用。
- 单文档内容修订：1 个文档重算、24 个文档复用，集合无交集且覆盖全部文档。
- SIGKILL 前已提交批次 `[0, 1]`；恢复时跳过 `[0, 1]`，仅执行 `[2, 3, 4]`。

实际耗时和查询毫秒数会随机器波动；完整最新观测见 `reports/acceptance_report.json` 与 `reports/ACCEPTANCE_REPORT.md`。
