# Local SFT Data Pipeline

一个完全本地、确定性、可由中断恢复的 LLM 指令微调数据构造应用。应用把一次数据构造建模为六个可提交阶段，使用不可变快照、全局单调版本、内容寻址缓存和原子 manifest 更新，保证中断恢复、编辑重算和最终导出的一致性。

不依赖网络、账号、外部服务或容器。运行时仅需要 Python 3.10+ 与 NumPy，其余均为标准库。

## 快速开始

### Windows

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m sft_pipeline init --reset-state
.\.venv\Scripts\python.exe -m sft_pipeline run --json
.\.venv\Scripts\python.exe -m sft_pipeline status
.\.venv\Scripts\python.exe -m sft_pipeline check
.\.venv\Scripts\python.exe -m sft_pipeline demo --kill-stage quality --mode kill
.\.venv\Scripts\python.exe -m sft_pipeline interactive
```

`demo --mode term` 在 POSIX 使用 `SIGTERM`；在 Windows 使用本地 `state/stop.request` 作为可处理停止信号。`--mode kill` 使用 `TerminateProcess`，模拟无法捕获的强制终止。

### Linux / macOS

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m sft_pipeline init --reset-state
python -m sft_pipeline run --json
python -m sft_pipeline demo --kill-stage quality --mode kill
python -m sft_pipeline interactive
```

也可以直接执行固定种子生成器：

```bash
python scripts/generate_data.py --root . --force
```

## 六阶段流水

| # | 阶段 | 输出 |
|---|---|---|
| 0 | `ingest` | 按 `sample_id` 排序并校验后的指令样本 |
| 1 | `normalize` | 模板渲染文本、模板编号、每个占位符的字符区间 |
| 2 | `quality` | 长度/重复率/噪声比例、加权质量分、质量档 |
| 3 | `dedupe` | 签名、相似邻居、连通簇、代表样本、保留/剔除结论 |
| 4 | `sample` | 每来源与质量档的配额计划、实际达成、欠配原因、采样种子 |
| 5 | `export` | 带版本头、内容校验和与整文件校验和的 JSONL |

只有阶段全部样本处理完成才会提交。阶段内的中间结果仅驻留内存，强制终止后从该阶段起点重建。

本地输入位于 `data/`：

- `samples.jsonl`：`sample_id, instruction, input, output, source_tag, quality_flag, lang, annotation_revision`
- `templates.json`：模板库版本、稳定哈希路由参数、模板编号/来源/模式/启用状态
- `cleaning_rules.json`：质量权重、长度目标、噪声字符、质量档、去重门限与签名参数
- `quotas.json`：采样种子、总量、来源/质量档目标
- `pipeline_config.json`：受控语言、必填字段、导出版本和资源上限

## 常用操作

```bash
# 查看当前阶段、版本、各阶段计数、最近编辑/恢复/错误报告
python -m sft_pipeline status

# 全链路一致性检查
python -m sft_pipeline check --json

# 修改单条样本输出，随后自动重算并输出受影响/复用集合
python -m sft_pipeline edit-output S0001 --output "新的回答内容"

# 调整清洗权重
python -m sft_pipeline edit-weight noise 0.2

# 删除或添加模板
python -m sft_pipeline remove-template T-GEN-B
python -m sft_pipeline add-template \
  --template-json '{"template_id":"T-NEW","source_tags":["general_en"],"pattern":"Q: {instruction}\nC: {input}\nA: {output}","enabled":true}'

# 改变来源/质量档配额或总量
python -m sft_pipeline edit-quota general_en high 4
python -m sft_pipeline edit-total 28
```

编辑命令会原子写回输入文件，然后自动运行增量传播。完整报告写入 `state/last_edit_report.json`。

## 确定性口径

### 模板路由与渲染

- 模板按 `template_id` 排序，所有启用且匹配 `source_tag` 的模板为候选。
- 以 `sha256(selection.salt|sample_id|template_id)` 的最小值选择模板，平局再用 `template_id` 打破。
- 占位符仅允许 `{instruction}`、`{input}`、`{output}`，三者均必须存在。
- 填充位置使用 Unicode 码点偏移，记录 `slot/start/end/length/value_sha256`。

### 质量分

质量分基于样本 `output`：

- `length_score = min(1, output_char_length / length_target)`
- `repetition_score = 1 - repeated_ngram_ratio`
- `noise_score = 1 - noise_char_ratio`
- 三个分量按配置权重加权后除以权重和，四舍五入到 6 位小数。
- 质量档按配置中从高到低的 `min_score` 阈值选择。

### 近重复签名

- 对 `output` 做 NFKC、转小写、空白折叠。
- 生成字符 5-gram，对每个 shingle 使用 BLAKE2b-64（person=`sft-sig1`）取 64 位哈希。
- 去重后排序，取最小的 64 个哈希作为 bottom-k 签名。
- 两个样本的相似度为签名集合的 Jaccard 相似度，阈值默认 `0.88`，达到阈值即连边。
- 相似图按连通分量成簇；每簇保留质量分最高者，同分保留 `sample_id` 最小者。
- 每个剔除记录包含所有达到门限的直接邻居、相似度、门限、簇编号、代表样本及簇内完整边/分数明细，可独立复算。

### 配比采样

- 原始配额声明之和与 `total_target` 不一致时，按最大余数法确定性缩放到总量。
- 每个来源/质量档内按 `quality_score desc`、种子哈希、`sample_id asc` 排序。
- 若部分目标候选不足，优先用其他未选候选补足全局总量；每目标仍记录计划量、实际量、缺口和原因。
- 导出行按配额目标声明顺序，再按目标内确定排名排列。

## 状态与缓存

```text
state/
  manifest.json                 # 原子替换的提交指针与 next_version
  snapshots/<stage>/vNNNNNN.json
  cache/objects/<content_sha256>.json
  cache/keys/<cache_key>.json
  locks/pipeline.lock
  locks/commit.lock
  recovery_report.json
  graceful_stop.json
  last_edit_report.json
  last_error.json
exports/dataset-<file_bytes_sha256>.jsonl
```

- 每阶段快照整体写入，永不增量修补；全局版本单调递增。
- 导出文件按整文件 SHA-256 命名，避免不同头部配置覆盖旧内容。
- manifest 采用 `临时文件 + fsync + os.replace` 原子可见。
- 缓存键包含算法版本、阶段、上游产物哈希、模板库、清洗权重、去重门限与配比目标摘要。
- 恢复前会逐阶段核对缓存键、上游校验和与 manifest 指针；最早失配阶段及之后快照在同一原子 manifest 更新中失效。
- 提交使用非阻塞进程锁；锁被占用立即返回 `E_STAGE_BUSY`，死进程遗留锁可自动回收。

## 业务错误码

| 码 | 含义 |
|---|---|
| `E_DUP_SAMPLE` | `sample_id` 重复 |
| `E_LANG_INVALID` | `lang` 不在受控集合 |
| `E_TEMPLATE_INVALID` | 模板结构、编号或占位符非法 |
| `E_CONFIG_INVALID` | 权重、门限、配额等配置非法 |
| `E_INPUT_INVALID` | 必填字段、类型或修订号无法解析 |
| `E_RESOURCE_LIMIT` | 样本数或估算驻留内存超限 |
| `E_STAGE_BUSY` | 并发提交/操作冲突 |
| `E_STAGE_ORDER` | 阶段顺序或提交前提不满足 |
| `E_SNAPSHOT_ROLLBACK` | 试图用旧版本覆盖已提交阶段 |
| `E_SNAPSHOT_CORRUPT` | 快照/ manifest 校验和不符或损坏 |
| `E_CACHE_CORRUPT` | 内容寻址缓存对象损坏 |
| `E_STATE_INCONSISTENT` | 跨阶段状态不一致 |

错误对象包含 `code`，并尽可能携带 `field`、`stage`、`sample_id` 与 `details`。

## 资源上限

`data/pipeline_config.json` 的 `limits` 配置：

- `max_samples_per_stage`
- `max_resident_memory_mb`（基于阶段输入的保守估算）
- `max_parallel_stages`
- `requested_parallel_stages`

样本数或内存估算超限返回 `E_RESOURCE_LIMIT`。并行请求超过上限时按明确策略降为上限值，并在运行报告 `degradations` 中标记 `parallel_stages_capped`。

## 测试

```bash
python -m unittest discover -v
```

测试覆盖：确定性字节对比、六阶段计数、去重/配额明细复算、导出头与校验和、四类编辑传播、资源限制、业务错误字段、快照回滚/损坏/并发、提交后强杀恢复、可处理信号边界退出、阶段中途强杀后丢弃半成品。

详细设计与实际验证记录见 `DESIGN.md` 和 `VERIFICATION.md`。
