# DepRule 技术说明

DepRule 是一个仅依赖 Python 3.10+ 标准库的句法/依存规则互操作组件。它把 v1 竖线记录、v2 JSON 分片 + SQLite 元数据库、v3 嵌套校验和快照统一迁移为确定性 IR，并在同一 DAG 中完成解析、缓存、失效传播、重放、检查点恢复和版本提交控制。

## 快速命令

```bash
python3 -m deprule.cli seed --out data
bash scripts/demo.sh
python3 -m unittest discover -s tests -v
```

常用单命令：

```bash
python3 -m deprule.cli verify-equivalence \
  --tasks data/tasks.jsonl \
  --v1 data/rules-v1.json \
  --v2 data/rules-v2/v2_manifest.json \
  --v3 data/rules-v3.json

python3 -m deprule.cli parse \
  --tasks data/tasks.jsonl \
  --rules data/rules-v1.json \
  --state-dir artifacts/local-state \
  --out-dir artifacts/local-run --fresh
```

## 业务码

- `OK`：业务成功。
- `E_VERSION_UNSUPPORTED`：版本头无法分派。
- `E_FORMAT_CORRUPT`：字段、竖线数、正整数或标签数组不合法。
- `E_SEMANTIC_DRIFT`：IR、跨格式解析或重放结果出现非预期差异。
- `E_STALE_RESULT`：导出依赖的 token、规则、标签集、根策略或上游哈希已变化。
- `E_SNAPSHOT_ROLLBACK`：旧 revision 试图覆盖新 revision。
- `E_STAGE_BUSY`：非阻塞提交锁已被其他提交者持有。
- `E_TOKEN_VALIDATION`：任务 token/POS/raw_text 一致性校验失败。

CLI 对可判定业务错误输出 JSON 并返回退出码 `2`，`E_INTERNAL` 返回 `1`；强制崩溃演练使用 `os._exit(43)` 模拟未提交阶段被终止。

## 规则读取与迁移

读取入口是 `deprule.rules.read_snapshot(path)`。目录自动读取 `v2_manifest.json`；有版本头时按版本分派；无版本头时按 `pipe_record`、`shards + meta_db`、`rule + checksums` 字段集分派。

v1 `pipe_record` 固定 11 个竖线字段：

```text
V1|rule_id|revision|syntax_tags|dependency_labels|directions|root_strategy|pos_priority|fallback|search_window|root_label
```

读取器按顺序报告第一个失败字段，例如 `version`、`pipe_record[4]`、`pipe_record[9]`、`rule.syntax.tag_set[1]`、`checksums.root_policy`。

v2 使用 `tags.json`、`direction.json`、`root.json` 和 `rules-meta.sqlite`。SQLite 保存 `rule_meta` 与 `shard_checksum`，读取时复核分片哈希。v3 逐项验证标签集、方向映射、根策略和声明封印校验和。

IR 派生 `tag_set_version`、`label_set_version`、`direction_version`、`root_policy_version` 和 `semantic_hash`。v1 升级时补全方向幂等约束、根回退链、标签集合校验和及默认约束，并在 `provenance`/`completion_sources` 记录来源。v3 降级到 v1 时，嵌套方向分组可无损展平；语言覆盖和多余回退链可按 `discard` 显式丢弃，动作写入 `downgrade_actions`。严格回读发现未授权差异时返回 `E_SEMANTIC_DRIFT` 和首个 `difference_field`。

## 确定性解析 DAG

节点顺序为：

```text
normalize -> preanalysis -> arcs -> labels -> root_crosscheck -> checksums -> export
```

四个检查点阶段是：

1. `normalize_validate`：NFKC、空白折叠、raw/token 重建校验、POS 长度校验和 token 哈希。
2. `preanalysis`：确定性句法标签、NP/S 短语集合和结构根候选。
3. `parse_arcs_labels`：基于短语头、POS 和方向契约生成唯一依存弧与标签。
4. `root_regression_crosscheck`：根策略选根、确定性 reroot，以及单中心、单父、无环、标签集和方向校验。

种子包含 6 个中英文句子。解析产物不使用随机数、UUID 或时钟；同 sentence_id、规则 IR 和配置下规范 JSON 字节一致。

## 缓存与定向失效

缓存位于 `<state>/cache/blobs/<node>/<sha256>.json` 和 `<state>/cache/entries/.../<cache_key>.json`。键材料包含原始 token 序列哈希、规则快照内容哈希、标签集版本、根节点判定版本、上游节点哈希和解析器配置版本。

影响关系：

- `syntax_tags` 影响 `preanalysis`、`arcs`、`labels`。
- `dependency_labels`、`direction` 影响 `arcs`、`labels`、`root_crosscheck`。
- `root_policy` 影响 `root_crosscheck`。
- 任意直接影响沿 DAG 传播到 `checksums`、`export`。

未受影响节点凭旧账本中的内容哈希复用语义产物，并以当前规则哈希重建物理键索引。根策略修订示例中，6 句共 24 个上游实例复用，18 个根/校验和/导出实例重算；增量结果与全新状态结果逐字节相同。账本同时输出 `linguistic_result_changed_sentence_ids`：依赖层面全部句子都重算根后缀，但排除版本元数据后，示例中实际弧/根变化仅 `en-001`。

## 等价裁决与重放

`adjudicate_equivalence` 先比较 IR 语义视图，再比较句法树集合、依存弧集合、标签、根、约束和阶段校验和。解析差异映射到 `arc_set`、`label_set`、`root_set`、`syntax_tree_set`、`constraint_set` 等类别，并给出 `difference_location`。

每条导出弧记录来源句法标签、`path_node_versions`、`path_node_cache_keys`、自身 `cache_key`、生产节点和 `root_decision_basis`。重放先比较 `version_chain`；任一依赖版本变化即返回 `E_STALE_RESULT`，详情含受影响句子 ID 和完整版本链。

## 检查点与并发版本库

检查点位于 `<state>/checkpoints/<pipeline_id>/stage-01..04.json`，包含单调版本号、`completed_sentences`、节点引用和 `content_checksum`，通过同目录临时文件加 `os.replace` 原子发布。恢复时找到连续已提交最高阶段，删除更高阶段的 `work/stage_N/` 和遗留临时文件；已提交阶段标记为 `recovered`，不重新执行解析函数。

`VersionedStore` 为规则和账本维护 `head.json` 与 `versions/<domain>-000001.json`。每次提交写完整新版本并原子换 head，记录单调 `version`、业务 `revision` 和文档校验和。旧 revision 提交返回 `E_SNAPSHOT_ROLLBACK`。提交锁使用 `fcntl.flock(LOCK_EX | LOCK_NB)`，竞争提交立即获得 `E_STAGE_BUSY`。
