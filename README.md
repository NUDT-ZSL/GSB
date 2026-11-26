# 本地倒排索引诊断引擎

面向索引构建、倒排压缩、配置代际迁移和故障恢复的确定性本地诊断工程。工程只使用本地文件、Python 标准库和 numpy，不依赖容器或外部网络。

## 快速开始

```bash
python3 --version                 # 需要 3.10+
python3 -m pip install -r requirements.txt

# 生成固定种子样本（v1/v2 配置、JSONL 批次、故障与恢复指令）
python3 -m index_diag generate --output samples --seed 20261127

# 查看 v2 配置
python3 -m index_diag config inspect --config samples/config/v2/manifest.json

# 完整构建
python3 -m index_diag run \
  --work work \
  --config samples/config/v2/manifest.json \
  --batch samples/batches/batch_0001.jsonl \
  --batch samples/batches/batch_0002.jsonl \
  --batch samples/batches/batch_0003.jsonl

# 查询词项、指定压缩块和最近业务码
python3 -m index_diag query --work work --term search
python3 -m index_diag block --work work --term search --index 0
python3 -m index_diag last-code --work work

# 跨模块一致性自检
python3 -m index_diag selfcheck --work work

# 交互入口
python3 -m index_diag shell --work work
```

交互入口支持：

```text
postings <term>
blocks <term>
block <term> <block-index>
status
selfcheck
last-code
quit
```

## 配置迁移

两代格式都通过版本头定位解析器。v1 为单个 JSON；v2 为 manifest JSON、分片 JSON 和 SQLite 元数据库。

### v1 示例

```json
{
  "version": 1,
  "generation": 1,
  "logical_clock": 1001,
  "chunk_size": 4,
  "compression": "delta_zlib",
  "delta_step": 2,
  "vocabulary": ["search", "index", "query"],
  "resource_limits": {
    "max_batch_docs": 1024,
    "max_postings_bytes": 8388608,
    "max_comparisons": 5000000,
    "degrade_policy": "error"
  }
}
```

`resource_limits` 可省略，缺省字段会补齐并在 `field_sources`/SQLite 来源表中标记为 `default`。

### v2 布局

```text
config/v2/
├── manifest.json
├── metadata.sqlite
└── shards/
    ├── shard_000.json
    ├── shard_001.json
    └── shard_002.json
```

manifest 必须包含 `version: 2`、`metadata_db` 和非空 `shards`。SQLite 中：

- `config_meta(key, value)`：generation、逻辑时钟、分块参数、压缩模式、资源上限、迁移历史。
- `config_shards(shard_index, path, shard_clock, checksum)`：分片位置、逻辑时钟和内容哈希。
- `config_provenance(field, source)`：每个规范字段的来源，如 `v1:chunk_size`、`v2:shards`、`default`。
- `migration_history(seq, entry_json)`：升级/迁移审计记录。

分片文件必须包含 `version: 2`、`shard_index`、`shard_clock` 和 `vocabulary`。

迁移命令：

```bash
python3 -m index_diag config migrate \
  --source samples/config/v1/index_config.json \
  --target migrated-v2 --to-version 2 --shard-count 3

python3 -m index_diag config migrate \
  --source migrated-v2/manifest.json \
  --target downgraded.json --to-version 1
```

升级规则：

- 保留 generation、逻辑时钟、分块参数、压缩模式和词表。
- 补齐资源上限、迁移历史、分片与来源信息，并记录补全字段。
- v1 与升级后的 v2 在规范化参数上做回读校验，失败返回 `E_VERSION_UNSUPPORTED`。

降级规则：

- 分片词表按顺序展平为单一词表。
- 多个 shard clock 展平为 `max(logical_clock, shard_clock)`，并在报告中标记为 flattened。
- `metadata_db`、分片表、分片校验和、per-shard clock、来源表和迁移历史显式丢弃并写入报告。

## 文档批次格式

每行一个 JSON 对象，输入必须按 `(batch_no, doc_id)` 升序：

```json
{
  "batch_no": 1,
  "doc_id": 1001,
  "terms": ["search", "index", "search"],
  "term_frequencies": {"index": 1, "search": 2},
  "positions": {"index": [1], "search": [0, 2]},
  "doc_length": 3,
  "content_hash": "..."
}
```

支持的字段别名：

- 词项流：`terms`、`term_stream`、`tokens`
- 词频：`term_frequencies`、`term_freqs`、`tf`
- 位置：`positions`、`term_positions`
- 长度：`doc_length`、`length`
- 内容哈希：`content_hash`、`content_hash_hex`、`hash`

词项使用 NFKC、去首尾空白、casefold 规范化，且必须位于受控词表。`doc_length` 必须等于词项流长度和词频总和。位置若提供，必须与词项流推导结果完全一致。

## 运行语义

四阶段状态机：

```text
analyze -> postings -> compress -> ledger
```

每次阶段完成全部目标批次后才允许提交检查点。检查点使用临时文件、`fsync` 和 `os.replace` 原子替换，包含：

- 单调递增 `checkpoint_version`
- 当前阶段
- 各阶段已完成批次集合
- 完整工作快照
- payload 内容校验和与 envelope 校验和

恢复时加载最近已提交检查点。已提交的分析、倒排、压缩批次不会重新计入。若故障发生在阶段内、检查点提交前，该阶段会重新执行；若发生在检查点提交后，则从下一阶段继续。

故障与恢复：

```bash
# 在压缩检查点提交前注入故障
python3 -m index_diag run --work recovery-work \
  --config samples/config/v2/manifest.json \
  --batch samples/batches/batch_0001.jsonl \
  --fault samples/faults/fail_before_compress_checkpoint.json

# 按恢复指令从最近检查点继续
python3 -m index_diag recover --work recovery-work \
  --recovery samples/recovery/resume.json
```

故障文件支持：

```json
{"phase": "compress", "when": "before_checkpoint"}
```

`when` 可为 `before_checkpoint` 或 `after_checkpoint`。恢复文件示例：

```json
{"action": "resume", "clear_fault": true, "from_checkpoint": 2}
```

## 压缩与失效传播

支持模式：`raw`、`zlib`、`gzip`、`delta`、`delta_zlib`、`delta_gzip`。

差分步长 `delta_step = s` 的语义为：

```text
encoded[i] = value[i]                         i < s
encoded[i] = value[i] - value[i - s]         i >= s
```

每个压缩块独立重置差分链，因此可只解码指定词项的指定块。块内容哈希由模式、差分步长、整数个数和压缩 payload 共同计算。

词项整数流格式：

```text
每篇文档：[doc_type, doc_key, position_count, position...]
doc_type: 0=整数 doc_id，1=字符串 doc_id
doc_key: 整数 id；字符串 id 取 SHA-256 前 63 bit
```

缓存键包含：

- 词项内容摘要
- 配置 schema 版本和 generation
- chunk_size、compression、delta_step
- 上游倒排列表哈希

批次替换时只重建受影响词项；压缩阶段查找首个内容变化的块，复用前缀块、仅重算后缀。配置 generation 提升但压缩参数和倒排不变时，缓存条目可在新命名空间下复用全部块（rekey）。配置分块参数变化时全部块重算。每次失效生成复用/重算清单，保存在检查点和 `state/last_invalidation.json`。

## 账本与快照

`state/ledger.json` 是权威账本 bundle，单文件原子整体替换，内含：

- ledger 版本、entries、统计信息、逐批 digest
- 配置快照版本、generation、配置内容和校验和

`state/config_snapshot.json` 是便于外部读取的配置快照副本；权威副本仍嵌在 ledger bundle 中。

提交控制：

- 线程级非阻塞锁 + Linux `flock` 跨进程非阻塞锁。
- 同版本同内容提交幂等。
- 同版本不同校验和立即返回 `E_STAGE_BUSY`。
- 低版本提交返回 `E_SNAPSHOT_ROLLBACK`。
- 校验和错误或结构损坏返回 `E_SNAPSHOT_CORRUPT`。
- 后到并发提交立即返回 `E_STAGE_BUSY`。

## 资源上限

- `max_batch_docs`：单批文档数；`degrade_policy=truncate_batch` 时保留排序后的前 N 篇并记录动作，否则返回 `E_RESOURCE_LIMIT`。
- `max_postings_bytes`：驻留倒排规范化 JSON 大小。
- `max_comparisons`：二分插入排序比较次数。

不安全的静默截断不会用于倒排内存或比较次数；这两项超限始终报错。

## 业务码

| 业务码 | 含义 |
|---|---|
| `E_VERSION_UNSUPPORTED` | 版本头、配置字段、编码值或分片元数据非法 |
| `E_BAD_DOC` | 文档 JSON、doc_id、词项、长度或顺序非法 |
| `E_POSTING_INVALID` | 倒排重复、排序或统计不一致 |
| `E_COMPRESS_PARAM` | 分块大小、差分步长或压缩模式非法 |
| `E_RESOURCE_LIMIT` | 批次大小、倒排内存或比较次数超限 |
| `E_DUP_DOC` | doc_id 重复 |
| `E_BATCH_ROLLBACK` | 新批次号小于已提交批次 |
| `E_CONFIG_ROLLBACK` | 配置 generation 回退 |
| `E_SNAPSHOT_ROLLBACK` | 快照/检查点版本回退 |
| `E_SNAPSHOT_CORRUPT` | 账本、快照、检查点或压缩内容损坏 |
| `E_STAGE_BUSY` | 并发提交冲突 |
| `E_STAGE_INCOMPLETE` | 阶段批次未全部完成就尝试提交 |
| `E_TERM_NOT_FOUND` | 查询词项不在受控词表或缓存不存在 |
| `E_FAULT_INJECTED` | 按故障文件主动中断 |
| `E_IO` | 本地文件或锁 IO 错误 |

CLI 失败时输出 `{"ok":false,"code":...,"message":...,"details":...}`，并写入 `state/last_code.json`。

## 目录

```text
index_diag/
├── analysis.py       # JSONL 解析、词项规范化、确定性摘要
├── checkpoint.py     # 四阶段检查点
├── cli.py            # 命令行和交互 shell
├── compression.py    # 分块压缩、选择性解码、失效传播
├── config.py         # v1/v2 解析、规范化、双向迁移
├── engine.py         # 端到端状态机、恢复、批次替换
├── ledger.py         # 整体替换账本、快照、业务码日志
├── postings.py       # 增量倒排、排序、统计和自检
├── resources.py      # 资源上限与降级策略
└── samples.py        # 固定种子样本生成
scripts/
├── generate_samples.py
└── run_validation.py
tests/                # 32 个 unittest 测试
docs/DESIGN.md        # 不变量、原子性和恢复设计说明
```

## 验证

```bash
python3 -m unittest discover -v
python3 scripts/run_validation.py --report-dir artifacts
```

实际验证结果保存在：

- `artifacts/validation_report.json`
- `artifacts/unittest.txt`

当前结果：32 项测试通过；固定种子确定性、v1/v2 迁移与索引一致性、全部压缩模式、增量构建等价性、配置代际失效、批次替换后缀复用、故障恢复和关键业务码均通过。评测环境为 Python 3.10.12、numpy 2.0.0、WSL2 Ubuntu。
