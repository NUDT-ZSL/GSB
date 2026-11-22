# 本地 LLM 偏好数据构造工作台

这是一个不依赖网络、不依赖 Docker、只使用本地文件和进程池的确定性工作台。输入是确定性生成的偏好对 JSONL 与构造配置，流水为：

```text
质量打分 -> 近重复检测与分组 -> 对级一致性判定 -> 配比采样 -> 清单导出
```

实现同时覆盖：

- 乘加预算、驻留内存预算、worker 槽位三重约束下的准入与分批决策；
- 数据修正、质量权重变化、相似度门限变化后的内容寻址缓存与增量后缀重算；
- 局部增量结果与独立冷缓存全量结果的逐字节一致性；
- 配置/清单快照的单调版本、整体替换、过期结果拒绝和跨模块自检。

## 环境与快速开始

要求 Python 3.10+，固定 NumPy `1.26.4`。项目不要求容器或外部服务。

```bash
./scripts/setup_env.sh
./scripts/run_demo.sh
```

等价的手动命令：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

.venv/bin/python -m prefworkbench generate \
  --out data/sample.jsonl \
  --config configs/default.json \
  --count 120 \
  --seed 20261123

.venv/bin/python -m prefworkbench run \
  --input data/sample.jsonl \
  --config configs/default.json \
  --out runs/demo \
  --cache cache/demo

.venv/bin/python -m prefworkbench verify --run runs/demo
```

运行测试与完整验收：

```bash
./scripts/run_tests.sh
./scripts/run_acceptance.sh
```

验收报告写入 `artifacts/acceptance_report.json`，执行现场保存在 `artifacts/acceptance_work/`。

## 目录

```text
prefworkbench/
  generator.py     确定性样本与默认配置
  quality.py       加权质量分与进程池 worker
  neardup.py       分块签名、Jaccard、并查集聚类
  consistency.py   对级冲突与证据
  sampling.py      配额轮转采样与欠配归因
  scheduler.py     三重预算规划与裁决
  cache.py         内容寻址缓存
  snapshots.py     配置/清单快照与过期拒绝
  pipeline.py      五级流水、增量审计、清单导出
  selfcheck.py     跨模块一致性自检
  cli.py           命令行入口
scripts/
  setup_env.sh
  run_demo.sh
  run_tests.sh
  run_acceptance.sh
  verify_acceptance.py
tests/
```

## 数据与配置

JSONL 每条至少包含：

```json
{
  "pair_id": "P00000",
  "prompt": "...",
  "chosen": "...",
  "rejected": "...",
  "annotator_group": "G1",
  "quality_flag": "clean"
}
```

输入按 `pair_id` 排序后整体计算输入摘要。重复 `pair_id`、缺失字段、非字符串字段均返回结构化输入业务码。

配置分区包括：

- `quality`：三项权重、目标长度、pass 门限、质量档门限；
- `neardup`：分块 token 数、Jaccard 门限、`strict`/`tolerance` 策略；
- `consistency`：归一化方式和标注者一致性平滑系数；
- `sampling`：标注者组 × 质量档配额和可选总量上限；
- `scheduler`：worker 数、批次上限、MAC 预算、内存预算及代价模型；
- `precision`：当前支持 `fp64`；
- `priority_order`：预算冲突裁决优先级。

配置顶层 `version` 是单调递增快照版本。配置本体变化时必须提升版本；若版本相同而内容变化，返回 `E_CHECKSUM_MISMATCH`。

## 确定性口径

### 质量分

对每个回复：

```text
length_component     = min(1, len(text) / target_chars)
repetition_component = 1 - (token_count - unique_token_count) / token_count
noise_component      = 1 - noise_char_count / len(text)
response_score       = sum(weight_i * component_i)
```

token 口径为 Unicode `\w+` 并转小写。噪声字符包括 Unicode `C*` 类控制/格式/私用字符、替换字符、非允许标点/字母数字符号。每个分项都落盘：

```json
{
  "weight": 0.3,
  "raw_score": 0.9,
  "contribution": 0.27
}
```

pair 分为两侧分数的算术平均；质量档按配置顺序匹配，`pass` 由 `pass_threshold` 判定。质量排序键为 `(-pair_score, pair_id)`，同分时 `pair_id` 升序。

### 近重复

将 `prompt + chosen + rejected` 归一化为 NFKC + casefold + 空白折叠，再取重叠 token 窗口（长度 `chunk_tokens`），每个窗口以 BLAKE2b 8 字节哈希作为分块签名。pair 相似度为签名集合 Jaccard：

```text
similarity = |A ∩ B| / |A ∪ B|
```

`strict` 使用 `similarity >= threshold`；`tolerance` 使用
`similarity >= max(0, threshold - tolerance)`。相似边通过并查集聚类。簇代表按
`(-quality_score, pair_id)` 选择，簇内每个 pair 只出现一次，代表唯一。

规划器可选择全量 pair 比较、签名候选比较、候选半数/四分之一或关闭近重复比较。任何非全量方案都会在报告和 pair 路径上标记 `W_DEGRADED_GROUPING`。

### 一致性

同一 prompt 内检测两类独立证据：

- `chosen == rejected`：`E_IDENTICAL_SIDE_CONTRADICTION`；
- 同一文本在两条 pair 中处于相反侧：`E_LABEL_CROSS_CONTRADICTION`；
- 若相反侧还发生在同一标注者组，追加 `E_GROUP_DIRECTION_CONFLICT`。

每条冲突都记录 `prompt_digest`、两侧角色、文本摘要、pair_id、标注者组和冲突类型。组一致性使用配置平滑系数：

```text
smoothed_consistency = (non_conflict_pairs + smoothing)
                       / (pair_count + 2 * smoothing)
```

### 配比采样

只有同时满足以下条件的 pair 进入配额候选池：

1. 调度准入；
2. 质量 pass；
3. 不是冲突 pair；
4. 是近重复簇代表。

每个配额内按 `(-pair_score, blake2b(seed|group|tier|pair_id), pair_id)` 排序。各配额按 `(group, tier)` 排序后轮转取一条，优先保持总量推进。采样明细记录全局步号、配额、目标、rank 顺序哈希和 pair_id。欠配原因包括：

- `NO_ELIGIBLE_CANDIDATES`
- `ELIGIBLE_SUPPLY_BELOW_TARGET`
- `GLOBAL_TOTAL_CAP_REACHED`

## 三预算调度

设 pair 数 `n`、计划比较次数 `c`、批次数 `b`、worker 数 `w`、批规模 `s`：

```text
MAC    = n * mac_per_pair
       + c * mac_per_comparison
       + b * mac_reduction_per_batch
       + w * mac_worker_overhead

Memory = fixed
       + w * per_worker
       + w * min(s, n) * per_pair
       + c * per_comparison

EstTime = (n * mac_per_pair + c * mac_per_comparison) / w
        + b * mac_reduction_per_batch
        + w * mac_worker_overhead
```

规划器枚举分组质量档、worker 数和批规模。超额时从低优先级 pair 开始拒绝，直到预算可行。默认裁决优先级：

```text
progress > memory > grouping_quality > parallelism > wall_time > batch_efficiency
```

其中进度和内存必须位于前两位，内存是硬约束。提高 worker 可缩短估算时间但增加驻留内存和 worker MAC 开销；提高批规模可减少归约批次，但增加批次驻留内存。若必须牺牲分组质量来保留更多 pair，报告记录 `degrade_grouping`；若最小可行计划仍超 MAC，则拒绝 pair 并返回 `E_BUDGET_FLOP_EXCEEDED`。

每个 pair 在 `schedule_report.json` 和最终清单中有且只有一条路径：

```json
{
  "status": "accepted",
  "code": "W_DEGRADED_GROUPING",
  "batch_id": 3,
  "worker_slot": 1,
  "grouping_mode": "signature"
}
```

worker 内异常返回 `E_WORKER_FAILURE`，并带 `stage`、`batch_id`、`pair_ids`、异常类型和消息。整个批次死亡时，批次内所有 pair 都会被归因。

## 缓存、增量与快照

每个质量 pair 的缓存键包含：

```text
pair 输入摘要 + 质量阶段配置版本 + 采样种子 + 数值精度 + schema
```

每个全局级缓存键包含：

```text
数据集输入摘要 + 上游产物内容哈希 + 阶段配置版本
+ 采样种子 + 调度计划指纹/活动 pair + schema
```

这里的“配置版本”是阶段语义版本：只从该级实际依赖的配置分区计算。这样调整相似度门限不会使质量缓存失效，质量权重变化才会使质量阶段及下游失效。顶层快照版本和校验和另存于清单，并参与清单缓存键与新鲜度校验。

每个产物在 canonical JSON 上计算 `sha256:` 内容哈希并 sealed。缓存读取会重新校验哈希、缓存键和 payload 结构；损坏返回结构化业务码。

增量运行输出：

- `changed_input_pair_ids`
- 各级 `recomputed_pair_ids` / `reused_pair_ids`
- 各级 `affected_pair_ids`
- `recomputed_stages` / `reused_stages`

质量阶段可以部分重算；近重复、一致性、采样和清单是全局归约级，键变化时重算该后缀。局部增量与独立冷缓存全量运行最终都写同一套确定性清单，因此可逐字节比较。

配置快照：

- 版本必须单调递增；
- 同版本不同内容返回 `E_CHECKSUM_MISMATCH`；
- 回退版本返回 `E_VERSION_REGRESSION`；
- 新版本整体写新快照并原子替换当前指针；
- 清单携带版本、校验和、阶段配置版本；
- `check-stale` 在不一致时返回 `E_STALE_RESULT`；
- 新清单整体原子替换，旧清单不能覆盖新版本。

## 输出文件

`run` 目录包含：

- `quality.json`
- `neardup.json`
- `consistency.json`
- `sampling.json`
- `schedule_report.json`
- `manifest.json`
- `hashes.json`
- `diagnostics.json`

`manifest.json` 是最终业务清单，包含每个输入 pair 恰好一次的记录、状态、准入路径、质量证据、簇信息、冲突证据、采样明细、配额统计和各阶段哈希。`diagnostics.json` 额外包含缓存命中/未命中、增量级审计、是否部分重算、裁决依据和自检结果。

## 自检

`selfcheck.py` 校验：

- 每级重算集合与复用集合无交集；
- 重算 ∪ 复用覆盖全部活动 pair；
- 受影响集合被重算 ∪ 复用覆盖；
- 清单覆盖全部输入 pair 且无重复；
- 每 pair 恰属一个簇、簇代表唯一；
- 冲突参与者都有 conflicted 记录和证据；
- 调度路径覆盖全部 pair，准入集合与计划一致；
- 质量 rank 由分数降序、pair_id 升序重算；
- 采样序列无重复，配额统计可由明细重新加总。

## 业务码

| 业务码 | 含义 |
|---|---|
| `W_DEGRADED_GROUPING` | 为预算牺牲部分近重复分组质量 |
| `E_BUDGET_FLOP_EXCEEDED` | 最小可行计划仍超 MAC 预算，pair 被拒 |
| `E_BUDGET_MEMORY_EXCEEDED` | 内存预算裁决导致拒绝 |
| `E_WORKER_FAILURE` | worker 内异常，归因到阶段/批次/pair |
| `E_LABEL_CROSS_CONTRADICTION` | 同 prompt 交叉侧矛盾 |
| `E_GROUP_DIRECTION_CONFLICT` | 同标注者组标签方向冲突 |
| `E_IDENTICAL_SIDE_CONTRADICTION` | chosen 与 rejected 相同 |
| `E_STALE_RESULT` | 清单来自旧配置/旧依赖版本 |
| `E_VERSION_REGRESSION` | 尝试用旧版本覆盖新版本 |
| `E_CHECKSUM_MISMATCH` | 同版本配置或 sealed 内容校验和不一致 |
| `E_SELFCHECK_FAILED` | 跨模块自检失败 |

## 实际验证结果

本地验证环境：

- Python `3.10.12`
- NumPy `1.26.4`
- pytest `8.3.5`（仅测试依赖）

`artifacts/acceptance_report.json` 中 8 项检查全部通过：

| 检查 | 结果 |
|---|---|
| 确定性：基线、同缓存重跑、独立冷算 | 三者清单 SHA-256 均为 `0ee1e029ee485af54ebd9c294d7763d18305de5983f3bbf2fdca51cf8f4cdab0` |
| 预算裁决 | 压力预算选择 `1 worker / 16 batch / none`，拒绝 23 个 pair，含 `W_DEGRADED_GROUPING` 和 `E_BUDGET_FLOP_EXCEEDED` |
| 增量重算/复用 | 修正 `P00000` 后质量级重算 1 条、复用 119 条；近重复、一致性、采样、清单重算 |
| 逐字节一致性 | 增量与全量清单 SHA-256 均为 `88cc57e87e9326acb4d487f5d5702b10421b85983f6c9eec913c7279e9ba4103` |
| 门限变更后缀 | 质量级复用，近重复、一致性、采样、清单重算 |
| 过期/版本/篡改 | 分别得到 `E_STALE_RESULT`、`E_VERSION_REGRESSION`、`E_CHECKSUM_MISMATCH` |
| worker 异常 | `P99999` 在 `quality` 阶段、batch `17` 返回 `E_WORKER_FAILURE` |
| 自检与配额重算 | 选中 63 条，配额统计合计 63，所有清单 `selfcheck.ok = true` |

单元/集成测试结果：`8 passed`。

## 约束说明

- 运行期不访问网络，不使用真实账号或外部数据；NumPy 只在本地采样统计中使用。
- 所有业务输出不含时间戳、PID、绝对路径或未声明随机源。
- 进程池使用 Python 标准库 `ProcessPoolExecutor`，不依赖 Docker。
- `manifest.json`、阶段产物和缓存均采用 canonical JSON；人读文件也可直接用标准 JSON 工具打开。
