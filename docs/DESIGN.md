# 设计与一致性说明

## 1. 稳定序列化

所有校验和基于 UTF-8、按键排序、紧凑分隔符的规范 JSON：

```text
sha256(JSON(value, sort_keys=true, separators=(",", ":"), ensure_ascii=false))
```

源 JSONL 的每个对象在校验通过后记录 `__raw_checksum`，导出时直接引用。
这样不受源行排版、空格或键顺序影响。

## 2. 摄入与校验

`ingest` 先读取全部行，按 `(sample_id, line_no)` 排序，再检测重复 ID。
重复 ID 的所有出现均被拒绝，避免“保留哪一条”依赖输入行顺序。

字段边界来自策略 `field_limits`；`lang`、`source_tag`、`quality_flag`
来自受控集合。修订号支持：

```text
12
r12
rev-12
1.2.3
```

无点形式归一化为整数；语义版本形式归一化为
`major * 1_000_000 + minor * 1_000 + patch`。

阶段完成集合包含所有被扫描的唯一原料 ID，包括校验失败行；归一化样本表
只包含成功项。

## 3. 模板与质量

模板按声明顺序匹配 `lang` 和可选 `source_tags`。展开前用
`string.Formatter().parse` 检查：

- `instruction_template` 必须含 `{instruction}`；
- `input_template` 必须含 `{input}`；
- 引用的字段必须在允许占位符集合内；
- 不支持格式说明符和转换。

质量分：

```text
score = w_length * s_length + w_repetition * s_repetition + w_noise * s_noise
```

- 长度分在理想区间为 1，向绝对上下界线性衰减，越界为 0；
- 重复率 = `1 - unique_char_ngrams / all_char_ngrams`；
- 噪声率 = 规则字符集中字符数 / 文本长度；
- 重复和噪声分量分别在配置的 `zero_at_rate` 处归零。

每个分量显式输出权重、分量分、加权贡献和原始指标。总分保留 6 位小数，
质量档由 `tiers` 阈值决定。

## 4. 去重

规范文本先做大小写折叠与空白压缩，再取字符 shingle。对每个 shingle 用
固定盐的 BLAKE2b 计算 64 位哈希，构造 `Minhash Jaccard` 签名。

签名按 `band_size` 切块：

- 相同块进入同一桶，产生候选边；
- 候选边的相似度 = 相同签名行数 / 签名长度；
- 相似度 >= 门限的边做并查集合并；
- 每个样本最终恰属一个簇；
- 簇代表按 `quality_score desc, sample_id asc` 选择。

被剔除样本记录：

- 其所在簇和代表；
- 直接相似邻居；
- 相似度、门限、阈值比；
- 命中的签名块位置。

候选窗口 `candidate_windows` 按升序评估，选择不超过比较预算的最大窗口。
被拒绝的更大窗口保留 `E_COMPARE_BUDGET`、估算比较数、超出量和策略优先级。

## 5. 三重预算

`BudgetLimits` 包含：

- `max_workers`
- `compare_budget`
- `memory_limit_bytes`
- `error_priority`

批次估算包含样本数、比较代价、驻留内存和请求 worker 数。准入计划按批次
顺序累计比较数、记录峰值内存，并把拒绝项连同全部越界原因和预算值写回。
多个条件同时越界时，按 `error_priority` 选择主错误码。

worker 数选择从请求值向下回退，记录每个因内存被拒绝的候选值。即使发生
降级，阶段仍使用最大可安全承载的 worker 数。

worker 内业务异常保留原错误码；未知异常包装为 `E_WORKER_FAILURE`，并补全
`stage` 和 `batch_id`。

## 6. 配比采样

采样候选先按：

```text
sha256(seed:sample_id)
```

稳定排序。每个目标有 `source_tag + quality_tier + quota`：

1. 第一轮按目标声明顺序满足精确质量档，达到 `total_target` 即停止；
2. 若仍有总量余量且策略优先级允许，按最多 `max_tier_drop` 档从同 source
   的低质量档补足；
3. 每个目标输出实际达成量、种子、欠配原因；
4. 无法放宽或优先级拒绝时输出显式裁决。

欠配原因包括：

- `TOTAL_CAP_REACHED`
- `NO_EXACT_CANDIDATES`
- `SAMPLING_RANK_LIMIT`
- `QUALITY_RELAXATION_DENIED_BY_PRIORITY`
- `RELAXED_TIER_EXHAUSTED`
- `CONSTRAINED_UNDERFILL`

导出顺序严格采用目标声明顺序，再采用目标内种子排序。每行 `trace` 包含
原料行与校验和、模板 ID、质量贡献、簇 ID、目标 ID、采样种子及档位放宽
信息。

## 7. 阶段快照与恢复

每个阶段 JSON 快照包含：

```text
schema_version
stage
stage_index
snapshot_version        # 全局单调递增，每阶段 +1
parent_checksum
completed_sample_ids    # 排序去重
payload
checksum
committed_at_ms
```

`checksum` 覆盖除自身和提交时间外的全部内容。提交前验证已完成 ID 集合与
预期集合完全相等。写入使用同目录临时文件、`fsync` 和 `os.replace`。

恢复规则：

- 只读取 `stages/*.json`，忽略并清理 `.tmp-*`；
- 校验版本单调、阶段序号连续、父子校验和连续；
- 从最后一个已提交阶段的下一个阶段开始；
- 已提交阶段不执行、不重写；
- 未提交阶段没有持久化中间结果，因此从阶段起点重建；
- `run.json` 记录策略和输入文件校验和，变化后拒绝混用旧快照。

强制终止可由 `--fail-stage` 在计算后、提交前触发。CLI 返回
`E_FORCED_TERMINATION`，已提交快照保持可恢复。

## 8. 策略快照

### v1

- 单文件 JSON；
- 只有相对时间字符串；
- 无 `strategy_generation`；
- 无 `tombstones`；
- 提交锁为同目录 `O_EXCL` 文件，后到者立即 `E_STAGE_BUSY`。

### v2

- 每个配置顶层键一个 JSON 分片；
- 分片校验和写入 manifest；
- SQLite 保存历史快照、唯一活跃指针、提交 guard 和事件；
- `BEGIN IMMEDIATE` + `busy_timeout=0`，并发提交立即失败；
- 新快照写入独立 generation 目录，提交事务只切换活跃指针；
- 同一版本或新版本只要校验和变化，就整体替换活跃快照。

### 迁移

v1 -> v2：

- 补 `strategy_generation = 2`；
- 将 `retired_keys` 转为墓碑，没有时初始化为空；
- 相对时间转为绝对 Unix 毫秒；
- 在 `source.completed_fields` 记录补全来源。

v2 -> v1：

- v1 无法表达墓碑，因此不把墓碑写入 v1；
- 全部墓碑写入独立审计文件；
- v1 的 `migration_note` 记录 `V1_CANNOT_REPRESENT_TOMBSTONES`、
  丢弃数量、源校验和与审计校验和。

## 9. 并发边界

- 阶段状态锁防止两个进程同时提交阶段；
- 策略 v1 使用独占锁文件；
- 策略 v2 使用 SQLite 写事务和单行 guard；
- 只有一个 `active_snapshot` 指针；
- 后到策略提交返回 `E_STAGE_BUSY`，不会产生第二个活跃版本。

