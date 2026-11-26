# 技术说明

## 1. 数据与执行模型

输入 JSONL 每行必须包含 `sentence_id`、`raw_text`、`token_list`、`pos_tag_list`、`lang`、`domain`。句子按 `sentence_id` 的 Unicode 码点字典序处理。配置顶层 locale/domain 是默认值；每句的 lang/domain 可覆盖当前默认并参与缓存键与失效判断。任务 domain 必须在 `domain_tags` 中拥有受控标签集，否则返回 `E_FORMAT_CORRUPT` 并携带具体句子与字段。

流水固定为：

1. `normalize`：文本规范化。
2. `candidates`：词典与规则候选生成。
3. `disambiguation`：融合裁决与最终 token。
4. `tagging`：HMM 词性标注与一致性校验。

每个阶段在缓存中保存独立内容对象。阶段完成后的聚合 payload 进入检查点，阶段之间只通过内容哈希衔接。

## 2. 文本规范化

规范化先按每个原始码点做配置指定的 Unicode 形式（默认 `NFKC`），再按 locale/domain 配置执行 `casefold`/`lower`/保留大小写和领域单字符替换。连续空白按 `collapse` 规则归并为一个空格。

内部维护：

```text
normalized_text: str
char_map[i] = [raw_start, raw_end]
```

`char_map[i]` 表示规范化后的第 `i` 个码点来自原文的码点跨度。一个字符被 NFKC/casefold 扩成多个码点时，各输出码点共享同一原文跨度；连续空白归并时，一个规范化空格覆盖整段原文空白。最终 token 同时记录规范化偏移 `start/end` 与原文偏移 `raw_start/raw_end`。

校验同时要求：

- `"".join(token.text) == normalized_text`
- 规范化跨度从 0 开始连续且无交叉
- 原文跨度从 0 开始连续且最终覆盖 `len(raw_text)`

因此“逐字覆盖原文”与“按规范化文本分词”可以同时成立。

## 3. 候选生成与消歧

词典候选为当前位置开始的精确命中；规则候选由固定正则扫描产生，并区分 `prefix_rule` 与 `suffix_rule`。每个候选记录：

```text
start, end, text, source, source_version, source_id, tag_hint, priority
```

完全没有被任何词典或规则候选覆盖的码点会生成单字 `unknown` 候选。最终消歧从左到右贪心覆盖，对同一位置候选按以下稳定键排序：

```text
(-priority, length, text_codepoints, source, source_id)
```

默认优先级为词典 `300`、前缀规则 `200`、后缀规则 `100`。因此精确词典优先；同来源同分时较短词形优先；仍相同时按码点字典序。任何最终仍未覆盖的码点由消歧器补为单字 token，`source=unknown`。

## 4. HMM 词性标注

活动领域的受控标签来自 `domain_tags[domain]`，必须包含 `UNK` 与 `WS`。标签器从词典/规则 `tag_hint`、领域词性表和默认发射权重构造每个 token 的发射分布；HMM 的初始分布和转移分布从 `tagger` 配置读取并归一化。

- 用 Viterbi 求全局最优标签路径。
- 用 forward-backward 求每个位置所选标签的后验概率，作为 `confidence`。
- 所有浮点结果在校验前固定到 8 位小数，保证串并行字节一致。
- 若标签不在受控集合，返回 `E_TAG_INVALID`；若 token 有交叉、缺口或文本不符，分别返回 `E_TOKEN_OVERLAP` / `E_COVERAGE_MISMATCH`。

## 5. 配置 v1 与 v2

v1 是单个 JSON，包含 `format_version: 1` 和完整业务配置；若存在 `content_checksum`，读取时对移除该字段后的规范化 JSON 做 SHA-256 校验。

v2 目录结构：

```text
config.json          # 版本头、metadata_db、分片路径与 SHA-256、manifest_checksum
metadata.sqlite3     # meta(key,value,provenance) + shards(name,path,sha256)
core.json
dictionary.json
rules.json
tagging.json
extras.json
```

SQLite 的 `meta` 表逐项保存词典版本、规则版本、worker 数、批量大小、逻辑时钟与配置代数，并记录每项来源。读取 v2 时同时验证 manifest 校验和、各分片校验和以及 SQLite 必需字段。

迁移规则：

- v1 → v2：保留业务字段，补齐 v2 容器、默认可选字段、分片/清单校验和与 `metadata_extensions`；`audit` 逐项记录 `preserve`、`fill` 或 `compute` 来源。
- v2 → v1：`metadata_extensions`、`migration_audit`、`shard_integrity`、`shard_provenance`、`schema_version`、`snapshot_id` 等 v1 无法表达的字段按 `V1_DROP_RULE` 显式丢弃；迁移报告列出每个丢弃项和规则。
- 未知版本返回 `E_VERSION_UNSUPPORTED`；缺字段、类型错误、不可解析整数和校验和不符返回 `E_FORMAT_CORRUPT`。

## 6. 内容寻址缓存与定向失效

缓存目录：

```text
cache/objects/<hash[:2]>/<hash>.json   # 纯内容对象
cache/keys/<keyhash[:2]>/<keyhash>.json
```

对象文件以规范化 payload 的 SHA-256 命名；key 文件只保存完整 key 与对象哈希。阶段 key 固定包含：

```text
stage, sentence_id, raw_text_hash,
dictionary_version, rule_version,
config_parameter_version, stage_config_hash,
upstream_hash
```

运行清单保存每句的原始哈希、规范化文本、词典候选依赖、规则 ID、候选重叠数、四阶段 key/对象哈希和最终哈希。下一轮运行比较旧配置与新配置：

1. 计算词典条目语义差异，收集旧词形和新词形两侧的所有变更词。
2. 若变更词出现在原文/旧规范化文本，或命中旧句子依赖，则该句候选阶段起失效。
3. 对变更规则，同时检查旧依赖 ID、旧/新正则是否命中文本。
4. 消歧配置变化只失效存在同位候选竞争的句子；标注配置变化从标注阶段失效。
5. 规范化配置变化从规范化阶段失效；原始文本变化从规范化阶段失效；新增句子全阶段计算。
6. 仅 generation/worker/batch/logical-clock 变化时，语义产物全部复用，并在报告中标记 `scheduling_metadata_only`。

未失效阶段会把旧 key 别名到按新配置计算出的 key，指向同一内容对象，不重新计算。失效句只重算“首个失效阶段及其下游后缀”。报告输出：

```text
affected_sentences, reuse_sentences, reasons,
first_invalid_stage, invalidated_stages, reused_stages
```

## 7. 检查点、原子提交与恢复

每个阶段按确定性批次写 `state/pending/<stage>.json`，其中包含批次号、累计句子集合和待提交 key 哈希。pending 不是已生效检查点。

只有阶段全部句子成功、所有阶段 key 已写入内容缓存后，才在 `state/checkpoints/<stage>.json` 原子替换已生效检查点。检查点包含：

```text
stage, version, completed_sentence_ids, content_checksum,
payload_hash, task_digest, config_fingerprint, logical_clock_ms
```

`version` 由提交锁内的全局计数器分配，跨检查点与配置快照单调递增。原子写入采用临时文件、`fsync`、`os.replace` 和目录 `fsync`。

重启时：

- 已提交阶段直接读取检查点聚合 payload，不重算其中任何句子。
- pending 文件被删除；其列出的未提交阶段 key 同时回滚。
- 当前未提交阶段从上一已提交阶段结果重建。
- task digest 或 config fingerprint 不匹配的旧检查点不被沿用，但内容寻址缓存仍可通过定向失效复用未变化产物。

只有一个提交者能持有 `state/commit.lock` 的 `flock`。检查点与配置快照都经过该锁；竞争者使用 `LOCK_NB` 立即失败并返回 `E_STAGE_BUSY`。

## 8. 并行确定性

待计算句子按 sentence ID 码点序排序，再按 `batch_size` 切连续块。所有块按相同顺序提交给本地 `ProcessPoolExecutor`，结果按提交顺序读取和归并，不采用完成顺序。缓存命中项不改变剩余项的排序。

启用并行时，主流程还会对同一任务和配置执行一次独立串行参考。比较单位为每句：

```text
{sentence_id, tokens, pos_sequence}
```

逐句比较规范化 JSON 字节，并比较整个序列的聚合字节。发现差异立即返回 `E_PARALLEL_MISMATCH`，详情包含首个差异句、两侧 SHA-256 和聚合哈希。

## 9. 资源与错误码

资源检查顺序固定：

1. `workers > limits.max_workers`：`E_RESOURCE_LIMIT`，`priority=1`。
2. `batch_size > limits.max_batch_sentences`：`E_RESOURCE_LIMIT`，`priority=2`。
3. token 缓存内存估算超过 `max_token_cache_bytes`：按 `resource_policy` 返回 `E_RESOURCE_LIMIT`（`priority=3`），或降为单 worker 串行并记录 `serial_due_token_cache_memory`。

工作台没有常驻全局 token 池；内存估算基于原始文本长度与 `estimated_bytes_per_token` 的保守上界，用于在进入批次前执行确定的资源策略。

主要结构化错误码：

| 错误码 | 含义 |
|---|---|
| `E_PARALLEL_MISMATCH` | 并行与独立串行结果不一致 |
| `E_VERSION_UNSUPPORTED` | 配置版本头未知 |
| `E_FORMAT_CORRUPT` | 配置/任务字段、类型或校验和损坏 |
| `E_COVERAGE_MISMATCH` | token 未逐字覆盖或文本跨度不符 |
| `E_TOKEN_OVERLAP` | token 交叉或跨度非正 |
| `E_TAG_INVALID` | POS 标签不在领域受控集合 |
| `E_CHECKPOINT_CORRUPT` | 检查点结构、句子集合或版本损坏 |
| `E_CACHE_CORRUPT` | 内容对象或缓存 key 校验失败 |
| `E_STAGE_BUSY` | 检查点/配置快照提交冲突 |
| `E_RESOURCE_LIMIT` | 硬资源上限超限 |

## 10. 复现与验证

```bash
PYTHONPATH=. python3 -m unittest discover -s tests -v
PYTHONPATH=. python3 scripts/run_acceptance.py --output artifacts/acceptance
```

验收脚本使用固定种子 `20261127` 生成 24 句，执行串行、并行、v2、定向失效和强制中断恢复，最后写入：

```text
artifacts/acceptance/verification.json
artifacts/acceptance/diagnostic.html
artifacts/acceptance/parallel_output.jsonl
artifacts/acceptance/recovered_output.jsonl
artifacts/acceptance/sample/
artifacts/acceptance/*/state/
```

本轮实测：33 项测试全部通过；串行与并行输出 SHA-256 均为 `efb7f1ecd15d1688b545e81bb8868f8edad14003ec1796b19fcc50f2a62edeb7`；24/24 token 与 POS 参考命中；v1/v2 同哈希；删除“财报”后仅 7 句重算、17 句复用；消歧阶段中断后恢复输出与干净并行运行同哈希。
