# 偏好对齐数据构造诊断工作台

这是一个完全本地、确定性的 LLM 偏好对齐/奖励建模数据准备与诊断工作台。它从固定种子生成偏好对，运行五级构造流水线，在 `float64`、`float32`、定点缩放整数三档下比较误差传播，并通过内容寻址缓存执行增量失效重算与版本化快照。

不依赖 Docker、在线服务、账号或外部数据。运行时依赖 Python 3.10+ 与 NumPy；诊断页是自包含静态 HTML，不发起网络请求。

## 快速开始

当前验证环境为 Linux/WSL2：Python 3.10.12、NumPy 1.26.4、pytest 8.3.3。Windows 原生环境未在本执行器中实际运行；建议命令如下：

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"

python -m prefworkbench generate --out .workbench\data --seed 20261122 --count 42
python -m prefworkbench run --incremental
python -m prefworkbench serve --root .workbench --port 8765
```

打开 `http://127.0.0.1:8765/workbench.html`。`run` 的默认路径为：

- 样本：`.workbench/data/preference_pairs.jsonl`
- 配置：`.workbench/data/construction_config.json`
- 缓存：`.workbench/cache/`
- 快照：`.workbench/snapshots/`
- 诊断页：`.workbench/workbench.html`

一次性运行完整验收演示：

```bash
python -m prefworkbench demo --root .workbench-demo --count 42
python -m pytest -q
```

演示依次提交全量快照 v1、相似度阈值增量快照 v2、单样本修正增量快照 v3，再执行一次无缓存全量重跑，并比较 logic digest、误差谱、三档清单与参与集合。

## 项目结构

```text
prefworkbench/
  config.py        默认配置、合并与结构化校验
  generation.py    固定种子 JSONL、配置、签名参数生成
  textutil.py      分词、n-gram、重复率、噪声率
  signatures.py    BLAKE2b-64 bottom-k 签名与签名 Jaccard
  precision.py     float64/float32/定点算子层与舍入
  pipeline.py      五级流水、配额采样、缓存、增量归因
  errorspectrum.py 级联误差、区间、放大比、主导来源
  cache.py         每级内容寻址缓存
  snapshots.py     类别化快照、版本、校验和、整体替换
  report.py        自包含交互诊断 HTML
  cli.py           生成、运行、演示、本地服务
tests/             26 项自动化测试
```

## 确定性口径

### 质量打分

样本文本为 `prompt + "\n" + chosen + "\n" + rejected`。

- 长度分项：`min(1, char_count / length_target)`。
- 重复率：先做 Unicode NFKC、casefold、`\w+` 分词和 `shingle_size` 词级 n-gram；`repeat_rate = 1 - unique_ngrams / total_ngrams`。不足两个 n-gram 时重复率为 0。
- 噪声率：字符集合 `#@$%&*~` 的出现次数除以字符总数。
- 重复分项为 `1 - repeat_rate`，噪声分项为 `1 - noise_rate`。
- 配置权重先归一化：`nw_i = w_i / sum(w)`。
- 质量分：`q = sum(nw_i * component_i)`。

每个样本输出各分项的值、配置权重、归一化权重、贡献、存储表示和重算字段。定点档贡献和整数缩放值可在同一缩放域精确相加。

质量门限比较始终使用 `float64` 参考分；因此切换显示/算术精度不会改变样本参与集合。

### 近重复签名

1. 对样本文本做 Unicode NFKC、casefold 和 `\w+` 分词。
2. 构造词级 n-gram。
3. 使用 `BLAKE2b-64` 对每个 shingle 计算 64 位哈希，并取最低 `signature_size` 个不同哈希。
4. 相似度是两个 bottom-k 签名集合的精确 Jaccard：`|A∩B| / |A∪B|`。
5. 所有质量门限通过样本两两比较；候选对数超过上限时返回 `E_RESOURCE_LIMIT`。
6. 超过相似度门限的边组成连通分量；每分量保留质量分最高者，同分按 `pair_id` 升序打破平局。

相似度判定同样在 `float64` 参考域完成。三档精度只改变输出的相似度数值、去重权重和后续误差。

### 标注者一致性

对近重复保留集中的每个 `annotator_group`：

```text
mean = average(quality)
mad = average(abs(quality - mean))
raw = 1 / (1 + mad_scale * mad + epsilon)
denominator_raw = sample_count + smoothing_alpha
denominator = max(denominator_raw, min_denominator)
consistency = (sample_count * raw + smoothing_alpha * prior) / denominator
```

当 `denominator_raw < min_denominator`、分母接近零或组内样本数为零时，标记 `smoothing_protection_engaged`。保护值进入实际计算，不只做展示。

### 配比采样

- 质量档由 `quality_bands` 的 `low/medium/high` 阈值确定。
- 对每个保留样本计算 `raw_weight = quality * dedup_weight * consistency`，其中 `dedup_weight = 1 - max_similarity`。
- 确定性优先级：`u = (BLAKE2b(seed|pair_id)+1)/(2^64+1)`，`priority = -ln(u) / max(raw_weight, 1e-15)`，同优先级按 `pair_id`。
- 在 group/quality 两类配额构成的运输网络上做确定性最大流，先最大化总量，再在单元格内按优先级取样。
- 欠配原因包括 `insufficient_group_candidates`、`insufficient_quality_candidates`、`insufficient_candidates`、`total_or_quality_quota_constraint`、`total_or_group_quota_constraint`、`quota_matrix_bottleneck`。
- 最终权重：`normalized = raw_weight / sum(selected raw_weight)`；定点档再以显式 scale/rounding 存储。清单记录采样种子、配额目标、实际量、分母和各级缓存键。

## 精度档与误差传播

三档共用 `PrecisionOps` 算子接口：

- `float64`：IEEE-754 binary64，64 位中间量通道。
- `float32`：每次算子后量化为 binary32，32 位通道。
- `fixed`：整数缩放 `raw = round(x * scale)`；乘除在整数域执行并按配置模式重新缩放；记录 64 位存储通道、128 位乘积位宽、小数位和饱和/下溢事件。

舍入模式：`half_even`、`half_up`、`toward_zero`、`floor`、`ceil`。`fixed_scale` 必须是 `[1, 10^12]` 内整数。

每级 `stage_widths` 记录通道位宽、乘积位宽、缩放因子、小数位、舍入模式、量化次数、次正规/下溢保护次数、饱和次数和观测范围。

误差谱以 `float64` 为参考真值，覆盖：

1. `weighted_scoring`：质量加权分。
2. `threshold_comparison`：`max_similarity - similarity_threshold` 的余量。
3. `consistency_smoothing`：一致性平滑权重。
4. `normalization`：采样归一化权重。
5. `quantization`：最终存储权重。

对每级和每样本输出：

- 绝对偏差与相对偏差。
- 误差区间 `[min(tier_value, reference_value), max(tier_value, reference_value)]`。
- 相对首个质量加权级的误差放大比。
- 每样本权重置信区间 `[ref - |tier-ref|, ref + |tier-ref|]`，下限截断为 0。
- 以最大相对偏差为依据的主导来源：`weighted_scoring`、`threshold_comparison`、`normalization`、`quantization` 或 `none`。
- 完整归因明细，包括各阶段参考值、精度值和偏差。

## 稳定性保护

- 严格比较：`value >= threshold`。
- 容差带比较：`|value-threshold| <= tolerance` 时 tie-to-pass，且标记保护介入。
- 门限判定固定在 `float64` 参考域；任何精度档都使用同一参与集合和级联顺序。
- 次正规保护：非零小量低于 `subnormal_floor` 时钳制到该 floor；定点量化为零时按保护策略钳制到最小 1 个缩放单位。
- 一致性平滑和采样分母都有配置化下限；逐样本记录 `*_protection_engaged`。

## 增量失效

每级缓存键是 `SHA-256(canonical_json({cache_schema, stage, material}))`。stage material 包含精度 profile、该级配置、上游内容键、样本/签名/判定摘要。缓存文件同时保存 stage、key 和 payload checksum。

运行前对比上一快照的：

- 配置分区哈希；
- 每样本内容哈希；
- 每精度每级缓存键。

若键相同且缓存命中，该级标记为 `reused`；否则标记为 `recomputed`，并比较新旧级产物中的逐样本摘要，输出受影响 `pair_id` 集合。典型行为：

- 只调整相似度门限：`quality` 复用，`dedup → consistency → sampling → manifest` 重算。
- 调整质量权重或修正单样本：从 `quality` 开始的后缀全部失效。

增量运行和 `--fresh` 全量运行使用同一计算函数、同一缓存口径和同一误差谱函数。测试比较两者的 logic digest、误差谱 checksum 与三档清单 checksum，要求逐字节一致。

## 快照与资源限制

快照类别固定为 `manifest`、`statistics`、`error_spectrum`。每个类别包含：

- `category`
- 单调递增 `version`
- 类别 `checksum`
- payload checksum 与完整 payload

业务 `logic_digest` 相同且 manifest/error_spectrum 类别 payload 不变时保持原版本，即使本次仅是执行/失效元数据变化；任一业务类别变化时写入全新 `snapshot-NNNNNN.json`，随后原子替换 `latest.json`。不进行跨版本增量补丁。

`attempt_rollback()` 总是返回 `E_SNAPSHOT_ROLLBACK`；旧版本仍可只读加载。类别 payload、类别 envelope、整体 checksum 或 latest pointer 损坏时返回 `E_SNAPSHOT_CORRUPT`，details 指明 `category` 与 `version`。

可配置资源上限：

- `limits.max_samples`
- `limits.max_signature_candidates`
- `limits.max_manifest_bytes`

超限统一返回 `E_RESOURCE_LIMIT`，不做静默截断或改变权重口径的降级。

## 业务错误码

| 代码 | 含义 |
|---|---|
| `E_CONFIG_WEIGHT` | 质量分项权重非正/非有限 |
| `E_CONFIG_THRESHOLD` | 质量或相似度门限越界，或容差非法 |
| `E_CONFIG_SMOOTHING` | 平滑系数、prior、分母下限或 epsilon 非法 |
| `E_CONFIG_PRECISION` | 未知精度档 |
| `E_CONFIG_FIXED_SCALE` | 缩放因子非正或过大 |
| `E_CONFIG_ROUNDING` | 未知定点舍入模式 |
| `E_CONFIG_INVALID` | 其他配置结构/字段非法 |
| `E_SAMPLE_DUPLICATE_ID` | 重复 `pair_id`，排除全部同号记录 |
| `E_SAMPLE_EMPTY_FIELD` | `chosen` 或 `rejected` 为空 |
| `E_SAMPLE_INVALID` | 样本字段缺失、类型或格式非法 |
| `E_RESOURCE_LIMIT` | 样本数、签名候选数或清单估算内存超限 |
| `E_CACHE_CORRUPT` | 内容寻址缓存身份/校验和损坏 |
| `E_SNAPSHOT_CORRUPT` | 快照类别/整体校验和损坏 |
| `E_SNAPSHOT_ROLLBACK` | 尝试回退快照 |
| `E_SNAPSHOT_VERSION` | 请求的快照版本不存在 |

样本级错误逐条聚合，不中断其他样本；异常样本不会进入保留集、清单或误差统计。

## 诊断页

页面从持久化快照类别生成，包含：

- 三档质量分、一致性权重、最终权重分布；
- 五级误差传播链、阶段摘要、误差区间与放大比；
- 每样本主导误差来源与偏差明细；
- 保留集、剔除集、质量剔除、重复剔除、采样未选集合；
- group/quality/total 配额目标、实际量、欠配原因；
- 增量重算级、复用级、输入变化与失效覆盖；
- 最近业务错误码、样本异常和健康检查；
- 清单逐级追踪与权重独立重算；
- 三档精度切换和 `pair_id`/group/band 过滤。

## 实际验证记录

验证环境：Python 3.10.12、NumPy 1.26.4、pytest 8.3.3，Linux/WSL2；未使用 Docker，未访问网络目标。

执行：

```bash
python -m pytest -q
python -m prefworkbench demo --root .workbench-demo --count 42
```

结果：

- 自动化测试：`26 passed`。
- 全量 v1、阈值增量 v2、样本修正增量 v3 均成功提交，版本单调递增。
- 阈值增量 v2 的复用级为 `quality`，重算级为 `dedup → consistency → sampling → manifest`。
- 样本修正 v3 使 `quality → dedup → consistency → sampling → manifest` 失效，受影响 pair 集按内容差异输出。
- 增量 v3 与无缓存全量重跑：logic digest 相同、误差谱 byte-equal、三档清单 byte-equal、参与集合相同。
- 保留集与剔除集交集为空；清单 `manifest_selected_equals_retained`、`retained_excluded_disjoint`、`traceability_ok` 均为 true。
- 快照重复提交相同业务内容保持 version 3 且 `replaced=false`（logic digest 幂等）。

精确摘要写入 `.workbench-demo/verification.json`；测试覆盖快照类别损坏定位、回滚业务码、资源上限、重复 ID/空字段隔离、三档参与集一致、门限容差保护和平滑分母下限保护。

