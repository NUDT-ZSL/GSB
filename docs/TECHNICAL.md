# 技术说明

## 1. 总体结构

一次摘要改写被建模为四阶段 DAG：

```text
chunk_tokenize -> unit_score -> rewrite -> summary_merge
```

`support_summary` 各模块职责：

- `models.py`：会话、轮次、摘要单元、词表与配置模型。
- `tokenization.py`：受控词表最长匹配分词与确定性词频。
- `nodes.py`：四类纯计算节点。
- `cache.py`：内容寻址缓存、键构造和按会话审计。
- `pipeline.py`：DAG 执行、阶段快照接入、恢复和报告。
- `incremental.py`：三类定向失效场景。
- `snapshots.py`：四阶段快照、原子替换、非阻塞锁和错误防护。
- `validation.py`：输入、配置、词表使用和资源校验。
- `selfcheck.py`：跨阶段、评分、合并和节点分区自检。
- `recovery.py`：真实子进程强杀与恢复演示。

## 2. 规范化与确定性

所有哈希输入使用规范化 JSON：

- 键排序；
- 紧凑分隔符；
- UTF-8；
- `allow_nan=False`。

内容哈希为规范化 payload 的 SHA-256。缓存和快照均采用“临时文件写入 + `fsync` + `os.replace` + 目录 `fsync`”的原子替换。

精度档映射：

| 精度 | numpy dtype | 输出小数位 |
|---|---|---|
| `fp16` | `numpy.float16` | 4 |
| `fp32` | `numpy.float32` | 7 |
| `fp64` | `numpy.float64` | 12 |

分项贡献和总分统一量化，因此总分可由明细逐项独立重算。

## 3. 分块与分词

块大小表示“每块轮次数”。轮次排序后按固定步长划分，块边界保存为：

```json
{"block_index": 0, "start_turn_index": 0, "end_turn_index": 1, "turn_indices": [0, 1]}
```

分词规则：

1. 空白和中英文标点作为分隔符；
2. 分隔后的片段若本身是词表词条则直接接受；
3. 连续中文片段按词表做最长匹配；
4. 无法匹配的片段返回 `E_VOCAB_MISSING`，包含 `session_id`、轮次和字段路径。

词频排序为 `count` 降序，随后依次按首次出现的轮次、消息序号、词内位置和词条升序打破平局。

## 4. 摘要单元打分

每个单元计算四个稳定局部指标，避免追加轮次改变旧块打分：

- 位置：`1 / (1 + turn_index)`；
- 长度：`n / (n + 8)`；
- 词频：`(max_tf + smoothing) / (n + smoothing)`；
- 覆盖度：`distinct_terms / n`。

每项保存 component、weight、contribution；总分为 contribution 之和的精度量化值。排序按总分降序，再按 `turn_index`、`unit_index`、`unit_id` 升序。任一权重缺失或非正返回 `E_WEIGHT_INVALID`，`field` 指明 `weights.<name>`。

## 5. 改写与合并

改写保留原始词序，按平滑后的词频权重选择目标数量 token，优先保留 constraints 中的 `preserve_terms`，移除 `forbidden_words`，并遵守 `max_chars`。改写强度只影响此节点。

合并分两层：

- `summary_merge/block:<i>`：缓存每块的整数统计量和单元明细；
- `summary_merge/final`：由块产物哈希构造键并聚合。

归一化统计只依赖全局整数计数与集合：

- `source_token_count`
- `summary_token_count`
- 唯一源词集合
- 唯一摘要词集合
- 单元分数总和

因此块边界不会改变最终统计。每次合并还会构造一次性整段合并结果，并比较摘要文本、单元明细和质量指标的规范化字节。差异返回 `E_BLOCK_MISMATCH`，包含会话标识与首个字符差异位置。

## 6. 内容寻址缓存

对象路径：

```text
<cache>/objects/<key前两位>/<key>.json
```

对象包含：

```json
{
  "node_type": "rewrite",
  "session_id": "S001",
  "cache_key": "...",
  "key_components": {...},
  "content_hash": "...",
  "payload": {...}
}
```

读取时重新计算 payload 哈希，并与信封字段比对。

键中始终包含以下审计字段：`session_content_hash`、`vocab_version`、`rewrite_strength`、`smoothing`、`precision`、`upstream_hash`。字段值按节点相关性填 `null`，但结构稳定。

为了同时满足“会话内容哈希”和追加复用，`session_content_hash` 使用节点消费范围：

- 分块：截至当前块末轮次的会话前缀哈希；
- 打分：块内单元集合哈希；
- 改写：具体单元哈希；
- 合并：上游改写/块产物哈希组合。

追加只改变新的或尚未填满的末块前缀，因此已完成块保持原键。

## 7. 定向失效

| 变更 | 分块分词 | 打分 | 改写 | 合并 |
|---|---|---|---|---|
| 追加轮次 | 新块/被扩展末块重算，旧块复用 | 仅新块 | 仅新单元 | 受影响块与新 final |
| 仅改写强度变化 | 全复用 | 全复用 | 全重算 | 全重算 |
| 词表版本提升 | 全重算 | 全重算 | 全重算 | 全重算 |

合并键显式包含词表版本，避免词表版本提升但改写文本恰好相同时错误命中。每次增量运行还以独立临时缓存执行一次零复用全量运行，并比较完整摘要的规范化字节。

## 8. 阶段快照与恢复

快照文件：

```text
01_chunk_tokenize.json
02_unit_score.json
03_rewrite.json
04_summary_merge.json
```

信封字段：

```json
{
  "schema_version": 1,
  "stage": "rewrite",
  "version": 3,
  "completed_sessions": ["S001", "S002", "S003", "S004"],
  "run_signature": "...",
  "data": {...},
  "checksum": "..."
}
```

规则：

- 版本在所有阶段间全局严格递增；
- `completed_sessions` 必须恰好等于全部会话，否则 `E_SNAPSHOT_INCOMPLETE`；
- 校验和覆盖除 `checksum` 外的完整信封；
- 提交使用非阻塞 `O_CREAT|O_EXCL` 锁，冲突立即返回 `E_STAGE_BUSY`；
- 死进程遗留锁可通过 PID 存活检测清理；
- 低于最新版本的提交返回 `E_SNAPSHOT_ROLLBACK`；
- 文件损坏或校验和不符返回 `E_SNAPSHOT_CORRUPT`，包含阶段名与版本。

恢复时：

1. 加载最后连续已提交阶段；
2. 已提交阶段直接使用快照数据，不调用缓存、不重复计入；
3. 清除未完成原子写留下的临时文件；
4. 从首个未提交阶段开始，强制绕过该阶段及下游缓存，丢弃未提交中间状态并重建；
5. 最终摘要与完整运行进行逐字节比较。

`recovery-demo` 使用子进程和 `os._exit(97)` 实际强杀；父进程验证强杀后仅有阶段 1、2 快照，再启动恢复子进程。

## 9. 校验与资源

稳定业务码：

| 场景 | 业务码 |
|---|---|
| 精度档非法 | `E_PRECISION_INVALID` |
| 块大小非正 | `E_BLOCK_SIZE_INVALID` |
| 重复会话标识 | `E_DUPLICATE_SESSION` |
| 改写强度越界 | `E_STRENGTH_INVALID` |
| 词表缺失词条 | `E_VOCAB_MISSING` |
| 权重非正/缺失 | `E_WEIGHT_INVALID` |
| 块合并差异 | `E_BLOCK_MISMATCH` |
| 资源超限 | `E_RESOURCE_LIMIT` |
| 快照回退 | `E_SNAPSHOT_ROLLBACK` |
| 快照损坏 | `E_SNAPSHOT_CORRUPT` |
| 阶段并发冲突 | `E_STAGE_BUSY` |

资源上限：

- `max_sessions_per_stage`
- `max_resident_frequency`
- `max_intermediate_memory_bytes`

`resource_policy=error` 返回 `E_RESOURCE_LIMIT`；`degrade` 继续执行并在报告 `degradations` 中标记 `session_count`、`resident_frequency`、`intermediate_memory`。

## 10. 自检

`self_check` 检查：

1. 四阶段会话集合一致；
2. 每条打分可由 contribution 独立重算；
3. 合并结果可由逐单元明细独立重算；
4. 重算节点与复用节点无交集，且并集覆盖当前全部节点；
5. 恢复阶段节点单独标记为 `resumed_committed`。
