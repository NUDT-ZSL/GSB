# 设计说明

## 1. 目标与边界

本工程验证倒排索引在以下变化下仍保持一致：

- v1 单文件配置与 v2 分片配置之间的迁移；
- 增量批次追加；
- 批次替换；
- 配置 generation 提升或分块参数变化；
- 进程在阶段计算后、检查点提交前或提交后中断；
- 账本损坏、版本回退和并发提交。

所有输入由固定种子脚本生成，所有哈希使用 `ensure_ascii=False, sort_keys=True, separators=(",", ":")` 的规范化 JSON 或直接 SHA-256。状态中不写入时间戳，避免相同输入产生不同 checksum。

## 2. 模块边界

```text
config.py
  版本头分派 -> NormalizedConfig -> 单向/双向迁移

analysis.py
  JSONL 排序校验 -> 词项规范化 -> term frequencies/positions -> summary hash

postings.py
  doc_id/position 排序 -> 二分插入 -> 独立统计复算

compression.py
  整数流 -> 差分 -> zigzag varint -> 可选 zlib/gzip -> block hash
  cache key -> 前置块复用 -> 后缀重算 -> invalidation report

checkpoint.py
  阶段完成集合 -> 单调版本 -> payload/envelope 双 checksum -> 原子替换

ledger.py
  ledger + config snapshot bundle -> 整体替换 -> 非阻塞单写者锁

engine.py
  四阶段编排、检查点恢复、配置切换、批次替换、自检
```

## 3. 规范化配置

`NormalizedConfig` 是唯一进入索引、缓存和账本的配置形态：

```text
schema_version
generation
logical_clock
chunk_size
compression
delta_step
vocabulary
resource_limits
shard_clocks
field_sources
migration_history
```

`config_hash` 对上述 payload 计算，不把自身纳入哈希。`field_sources` 记录每个字段来源：

- `v1:<field>`：v1 原字段；
- `v2:metadata.<field>`：v2 SQLite 元数据；
- `v2:shards`：由分片合并；
- `default`：读取时补齐；
- `migration:v1_to_v2_default`：升级时新增的 v2 结构字段。

generation 是业务代数，schema_version 是格式代际。缓存键同时记录两者，避免同 generation 的 v1/v2 缓存误命中。

## 4. 文档和倒排不变量

对每个文档：

```text
doc_length == len(terms) == sum(term_frequencies.values())
positions[term] == [i for i, t in enumerate(terms) if t == term]
```

对每个词项：

```text
posting.doc_id 严格升序
posting.positions 严格升序
同一 doc_id 在每个词项列表中至多出现一次
```

每个 posting 的统计：

```text
doc_frequency       = len(postings)
term_frequency      = sum(len(posting.positions))
position_offset_sum = sum(sum(posting.positions))
position_checksum   = sha256([[doc_id, positions] ...])
```

`validate_postings_against_documents` 不读取已有统计作为真值，而是从文档词项流重建期望 posting，再与列表和统计逐项比较。任何偏差返回 `E_POSTING_INVALID`。

## 5. 压缩格式

整数流先做 lagged delta：

压缩模式支持 `raw`、`zlib`、`gzip`、`delta`、`delta_zlib`、`delta_gzip`。

```text
d[i] = v[i]                 i < step
d[i] = v[i] - v[i - step]   i >= step
```

随后做任意精度有符号 zigzag 和 unsigned varint。这样即使两个 int64 合法值的差超出 int64 范围，也能无损往返；解码后 final value 必须仍落在 int64。

差分链在每个块开头重置，因此：

```text
decode_block(term, i) 不依赖 block i-1
decode_term(term)     = concatenate(decode_block(...))
```

块哈希：

```text
content_hash = sha256(canonical({mode, delta_step, count}) + 0x00 + payload)
raw_hash     = sha256(canonical(raw_integers))
```

`raw_hash` 用于失效时判断前缀切片是否相同，避免为了复用而重新编码未受影响的块。

## 6. 缓存键与失效

缓存键：

```text
sha256(canonical({
  term_digest,
  config_version,
  config_generation,
  params: {chunk_size, compression, delta_step},
  posting_hash
}))
```

批次替换流程：

1. 移除目标批次旧文档，加入新文档。
2. `affected_terms = terms(old batch) ∪ terms(new batch)`。
3. 只重建 affected terms 的 posting list。
4. 对每个词项按 `chunk_size` 切片，从 block 0 开始比较 `raw_hash`。
5. 遇到首个不同块后，复用全部前缀，重算该块及之后所有块。
6. 未命中词项保持原缓存条目。
7. 输出 `affected_terms`、`untouched_terms`、`blocks_reused`、`blocks_recomputed`、`first_recomputed_block`。

配置 generation 提升但压缩参数与 posting 不变时，块内容无需变化，缓存条目只迁移到新 cache key，报告中记为 `rekeyed_terms`。分块参数变化时块边界或编码语义变化，全部块重算。

## 7. 四阶段检查点

阶段：

```text
analyze -> postings -> compress -> ledger
```

检查点 payload 包含：

```text
config
documents
postings
posting_statistics
compression cache
batch_info
pending replacement metadata
metrics
resource actions
compression/ledger config markers
```

envelope 包含：

```text
checkpoint_version
phase
completed_by_phase
expected_batches
payload
content_checksum = sha256(payload)
checksum         = sha256(envelope without checksum)
```

提交前检查：

- 当前阶段的 expected batches 全部在 completed 集合中；
- 更早阶段的 expected batches 全部完成；
- 检查点版本为前一版本 + 1；
- 通过非阻塞文件锁写入。

阶段内失败时检查点不变。若故障注入发生在提交前，引擎会先回滚内存状态到最近检查点再抛出。重启后从该检查点继续。

## 8. 崩溃窗口与幂等性

### ledger 提交后、checkpoint 提交前

账本 bundle 版本已增加，但检查点仍指向 `compress`。重启后：

1. 重新计算期望 ledger body；
2. 读取现有 ledger；
3. 若 entries/statistics/batch digests/config snapshot 相同，则复用该 ledger 版本；
4. 只提交检查点。

因此不会无限增加空账本版本。

### cache 文件与检查点

权威压缩缓存在检查点 payload 内。`cache/compression.json` 是查询便利副本，不参与恢复时的事实判断。即使该文件落后或缺失，检查点中的缓存仍可恢复；运行成功后会整体重写该文件。

### 配置切换中途失败

checkpoint payload 中分别保存：

- `compression_config_hash`：最近一次完成压缩时使用的配置；
- `ledger_config_hash`：最近一次完成账本提交时使用的配置。

只要二者与当前 config hash 不同，恢复时会强制重做相应阶段。文档分析或倒排阶段不因压缩参数变化而重做。

## 9. 批次替换的恢复

替换事务先在检查点中写入：

```json
{
  "pending_replacement": {
    "batch_no": 2,
    "affected_terms": ["..."],
    "old_doc_ids": ["..."],
    "new_doc_ids": ["..."]
  }
}
```

若在 `analyze` 检查点之后、`ledger` 检查点之前中断，普通 `run`/`recover` 会读取该元数据，在 postings 阶段重建 affected terms，在 compression 阶段复用未受影响块，并在 ledger 成功后清除 pending 标记。

## 10. 账本原子性

ledger bundle 是单个 JSON 文件：

```json
{
  "format": "index-diag-ledger-bundle",
  "format_version": 1,
  "version": 3,
  "body": {
    "ledger": {"version": 3, "...": "...", "checksum": "..."},
    "config_snapshot": {"version": 2, "...": "...", "checksum": "..."}
  },
  "checksum": "..."
}
```

envelope checksum 绑定 format、version 和完整 body；嵌套 ledger/config snapshot 各自还有独立 checksum。

`state/config_snapshot.json` 在 ledger 提交后重写为非权威副本，便于外部读取。

版本规则：

```text
new_version < current_version         -> E_SNAPSHOT_ROLLBACK
new_version == current, same body     -> idempotent success
new_version == current, diff checksum -> E_STAGE_BUSY
new_version > current                 -> whole-file replace
```

## 11. 资源限制

批量文档上限在批次分析前检查。`truncate_batch` 只允许用于文档数这一安全可审计的降级项，并记录保留/丢弃数量。

倒排驻留内存上限和排序比较次数不静默降级：

- 倒排大小按规范化 JSON 字节数计算；
- 二分插入的每次 doc_id 比较都计数；
- 超过任一上限返回 `E_RESOURCE_LIMIT`。

## 12. 自检

`selfcheck` 执行：

1. 文档词项仍在当前受控词表；
2. 从文档重建倒排并逐项比较；
3. 复算 doc_frequency、term_frequency、position_offset_sum、position_checksum；
4. 校验 batch_info 与文档内容；
5. 校验缓存参数、generation、schema version 和词项集合；
6. 解码每个词项并比较整数流；
7. 校验 ledger entries、batch digests、postings digest、cache checksum、配置快照；
8. 校验检查点各阶段完成集合覆盖全部文档批次；
9. 确认没有遗留 pending replacement。

失败统一返回 `E_SNAPSHOT_CORRUPT` 或 `E_POSTING_INVALID`，并指出具体词项、批次或阶段。

## 13. 验证覆盖

测试分为：

- `test_config.py`：v1/v2 往返、缺失字段、非法编码、分片 checksum、generation floor、降级展平/丢弃。
- `test_analysis_postings.py`：字节确定性、非法文档、重复 doc_id、批次回退、增量追加、排序、统计复算、资源限制。
- `test_compression.py`：全部模式、int64 边界、非法参数、块哈希、选择性解码、缓存键迁移、前缀复用和后缀重算。
- `test_checkpoint_snapshot.py`：检查点版本/阶段/内容校验和、账本整体替换、回退、损坏、线程锁和跨进程锁。
- `test_engine.py`：完整构建、查询、无操作重跑、增量批次、两种故障边界、配置代际升级、批次替换、错误码和自检。
- `test_cli_samples.py`：固定种子逐字节确定性、CLI 构建/查询/最近业务码/交互 shell。

验收脚本 `scripts/run_validation.py` 将结果写入 `artifacts/validation_report.json`，并保留完整 unittest 输出。
