# 摘要与改写精度误差传播管线

这是一个完全离线的 Python 3.10+ 项目。它从固定种子生成本地受控词表样本，
运行 `float64`、`float32` 与定点整数三档精度，输出逐句分数、摘要选择、
改写归一化、质量指标、逐级误差谱、门限诊断、版本快照和独立一致性验证结果。

项目不读取系统时间、不使用未固定随机源、不访问外部语料或网络服务。所有 JSON
输出使用稳定键序和固定小数位，完整结果由规范化 JSON 的 SHA-256 哈希标识。

## 环境与运行

本地验证环境：

- Python `3.10.12`
- NumPy `1.26.4`
- pytest `8.3.3`
- Linux 虚拟环境：`.venv`（`--system-site-packages --without-pip`，继承本地依赖）

Linux/macOS：

```bash
python3 -m venv --system-site-packages .venv
PYTHONPATH=src .venv/bin/python -m pytest -q
PYTHONPATH=src .venv/bin/python -m summarize_pipeline all
```

Windows 命令行：

```bat
py -3 -m venv --system-site-packages .venv
.venv\Scripts\python -c "import numpy, pytest; print(numpy.__version__, pytest.__version__)"
.venv\Scripts\python -m pytest
run_pipeline.bat all
```

也可以直接运行：

```bash
PYTHONPATH=src python3 -m summarize_pipeline all
PYTHONPATH=src python3 -m summarize_pipeline generate
PYTHONPATH=src python3 -m summarize_pipeline run
PYTHONPATH=src python3 -m summarize_pipeline validate --result artifacts/run.json
```

`all` 会生成 `data/samples.jsonl` 与 `configs/default.json`，运行三档精度，
写入 `artifacts/run.json`，提交整体快照，并执行两次完整复跑、校验和比对和
独立复算验证。

## 项目结构

```text
src/summarize_pipeline/
  config.py       配置解析、范围校验、结构化业务码
  generator.py    固定种子样本与默认配置
  precision.py    三档精度共用算子、定点量化与舍入
  pipeline.py     切分/词频/打分/选择/改写/质量与误差谱
  snapshots.py    整快照版本、内容校验和、回退保护
  validator.py    独立复算、跨级一致性、双跑哈希验证
  cli.py          命令行入口
tests/            28 个 pytest 验收用例
configs/          默认改写配置
data/             本地确定性 JSONL 样本
artifacts/        运行结果、验证结果、验收摘要与快照
```

`artifacts/cache/` 与 `artifacts/snapshots/` 已预留给本地缓存和快照写入；
当前运行已实际验证两个目录可读写。

## 输入模型

每行是一个已经切分好的句子单元，字段为：

```json
{
  "doc_id": "doc-0001",
  "sent_no": 0,
  "text": "anchor baseline target ...",
  "char_len": 73,
  "ref_summary_units": ["anchor", "target", "..."]
}
```

生成器先拼装完整文档，再通过确定性标点切分器重新得到句子，并校验切分数量。
加载器再校验字段完整性、`char_len == len(text)`、`sent_no` 从 0 连续、
同一文档参考单元一致和重复 `(doc_id, sent_no)`。坏样本只登记异常，不阻断
其他文档。

## 打分与选择语义

所有阶段使用同一个 `PrecisionOps` 接口。`float32` 在每个算子后重新转成
`np.float32`；定点档在每次算子后按显式缩放因子和舍入模式量化成整数，
再以 `raw / scale` 进入下一步。因此档位差异只来自精度损失，不改变算子顺序。

每个句子先得到四个分项：

- `position = (sentence_count - sent_no) / sentence_count`
- `length = min(char_len, ideal) / max(char_len, ideal)`
- `frequency = mean(normalized_term_frequency)`
- `coverage = |sentence_vocab ∩ document_vocab| / |document_vocab|`

词频归一化分母为：

```text
total_controlled_tokens + smoothing * controlled_vocab_size
```

加权和按固定顺序计算：

```text
w_position*position + w_length*length + w_frequency*frequency + w_coverage*coverage
```

随后执行平滑：

```text
smoothed = (weighted_sum + smoothing) / (1 + smoothing)
```

最终按 `smoothed` 降序、`sent_no` 升序排序；预算不足时跳过放不下的句子，
继续尝试后续句子。没有任一句入选时返回 `E_EMPTY_SELECTION`，异常明细中保留
每句分项、总分、门限判决和预算信息。

## 改写归一化与质量

选择阶段记录预算 `length_budget`、实际使用字符数和入选序列。改写阶段的
父分母显式记录为 `sum_selected_smoothed_scores`；若分母落入次正规数保护带，
改用 `subnormal_protected_uniform_floor` 并标记 `protected=true`。

平滑调整、温度和再次归一化：

```text
adjusted_i = (score_i + smoothing) / (selected_score_sum + n*smoothing)
powered_i  = adjusted_i ** (1 / temperature)
normalized_i = powered_i / sum(powered)
```

若温度分母低于保护带，改用均匀回退分母并记录来源。改写单元按归一化得分降序、
`sent_no` 升序拼接。质量指标：

```text
coverage = |reference_units ∩ output_tokens| / |reference_units|
compression = len(output_text) / len(original_text)
efficiency = max(0, 1 - |compression - target_compression| / target_compression)
quality = 0.7*coverage + 0.3*efficiency
```

聚合门限使用与句子、改写门限相同的比较策略。

## 门限稳定性

支持两种策略：

- `strict`：`value > threshold`
- `tolerance`：`value >= threshold - tolerance`

容差带把门限附近因舍入产生的微小左右漂移吸收到同一判决中。每个文档同时记录
严格判决和容差判决，报告受影响句子数、方向、`sent_no` 列表，并在存在差异时
标记 `POLICY_SENSITIVE`；否则标记 `STABLE`。所有算子的输出都经过
`subnormal_floor` 次正规数保护，除法另有受保护分母路径。

## 误差谱与归因口径

同一快照内以 `float64` 三档运行结果为参考真值。逐句比较六个阶段：

1. `frequency_norm`
2. `weighted_sum`
3. `smoothing`
4. `quantization`
5. `threshold_comparison`
6. `rewrite_normalization`

每级输出：

- 绝对偏差：`|actual - reference|`
- 相对偏差：`sum_abs_deviation / sum(|reference|)`
- 误差区间：该级逐句绝对偏差的 `[min, max]`
- 放大比：`sum_abs_deviation(k) / sum_abs_deviation(k-1)`
- 链式放大比：`sum_abs_deviation(k) / sum_abs_deviation(frequency_norm)`
- 主导来源：所有可追踪阶段中 `sum_abs_deviation` 最大者

放大比分母为零时：前后两级误差都为零记为 `1.0`；仅当前级非零时输出
`value=null` 且 `unbounded=true`。相对偏差参考分母为零时同样区分
“均为零”与 `relative_unbounded=true`。

每个聚合值都能追溯到 `per_sentence` 明细，明细含 `doc_id`、`sent_no`、
参考值、实际值、偏差、方向和定点 `raw`。定点明细还记录
`quantized_reference_raw`、`dequantized_actual` 与相对参考量化值的偏差，
可用缩放因子和舍入模式独立重算。

## 异常业务码

异常均为结构化对象，含 `code`、`stage`、`doc_id`、`message`、`detail`。

- `E_INVALID_WEIGHT`：任一打分权重不为正
- `E_INVALID_SMOOTHING`：平滑系数不在 `[0, 1]`
- `E_UNKNOWN_PRECISION`：未知精度档
- `E_INVALID_SCALE`：定点缩放因子非正
- `E_INVALID_ROUNDING`：未知舍入模式
- `E_DUPLICATE_DOC_ID`：重复文档/句子号
- `E_EMPTY_TEXT`：文本为空
- `E_MISSING_FIELD`：缺少必需字段，包括 `ref_summary_units`
- `E_EMPTY_SELECTION`：预算导致空选择，保留全部分项分数
- `E_UNATTRIBUTED`：未归因异常，统一登记
- `E_SNAPSHOT_ROLLBACK`：尝试加载或提交旧快照
- `E_SNAPSHOT_CORRUPT`：快照损坏、校验和不符或同版本内容被改写
- `E_INCONSISTENT`：验证器发现跨级不一致

## 快照版本管理

一个快照包含五个独立带版本与校验和的区段：

- `scoring_config`
- `smoothing_threshold_config`
- `error_spectrum_float64`
- `error_spectrum_float32`
- `error_spectrum_fixed`

每次提交写入完整 `current.json` 与 `history/vNNNNNNNN.json`，从不按区段增量
修补。总体版本只能前进；同版本同校验和幂等，同版本不同内容、区段校验和错误、
总体校验和错误均返回 `E_SNAPSHOT_CORRUPT`；低版本提交或加载返回
`E_SNAPSHOT_ROLLBACK`。高版本加载会整体替换当前快照。

## 独立验证

`validator.py` 不信任管线缓存，直接从输出重算：

- 各精度档及误差谱的 `doc_id` 集合一致
- 句号连续、分项加权和与平滑分数
- 句子门限、预算与选中序列
- 改写父分母、温度归一化和改写门限
- 选中句分项、定点量化值与参考量化偏差
- `rewritten_text`、`kept_tokens`、`ref_hits`
- 覆盖度、压缩率、效率和聚合质量
- 门限策略诊断计数
- 所有误差区间 `low_abs <= high_abs`

任一失败返回 `E_INCONSISTENT` 并给出类别与 `doc_id`。`verify_reproducibility`
连续运行两次完整管线，比较 `run_hash` 与完整规范化输出。

## 实际验收结果

本工作区最终使用 12 个文档、142 个句子运行：

- 三档均处理 12 个文档，无输入、样本或未归因异常
- 三档均选中 63 个摘要单元并产出 63 个改写单元
- `float64` 平均质量：`0.6728756560785`
- `float32` 平均质量：`0.672875650227`
- `fixed(scale=1e6, nearest_even)` 平均质量：`0.6728756666666666`
- 默认容差策略下三档门限影响数均为 0，标记为 `STABLE`
- 独立验证：`28 passed`，`issue_count=0`
- 两次完整运行哈希一致：`c144bb8aa4653fabad2382681d799f97db48184a539df036a5836ef80724f221`
- 配置校验和：`05f80baaf944eb024f3e7f44104e73c940060ecd6c3de9719dbe6517d2e6d267`
- 快照版本：`3`，校验和：`e76bdeeff9e244b56ff422c4af847091d0f34b68cefa767c9f4a59a3a9d9fd4d`

误差谱主导来源：

- `float32`：`smoothing`，最大绝对偏差 `9.8498e-08`，链式放大比 `23.663962461686`
- `fixed`：`smoothing`，最大绝对偏差 `1.440858e-06`，链式放大比 `1.989159804793`

完整数值见 `artifacts/acceptance_summary.json`、`artifacts/run.json` 和
`artifacts/validation.json`。测试用例位于 `tests/`，覆盖确定性字节输出、
三档切换、近门限稳定性、空选择、批处理异常、未归因异常、快照回退/损坏、
定点偏差重算、质量复算、选择复算和 CLI 双跑哈希。

