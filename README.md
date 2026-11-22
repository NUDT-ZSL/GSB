# Deterministic Tabular Feature Lab

面向本地表格样本的确定性特征编码、交叉展开、特征选择、内容寻址缓存、增量失效与异常诊断组件。项目不依赖外部数据集或在线服务，所有样本、配置、缓存、快照和验收证据都由本地文件生成。

## 快速开始

```bash
uv python install 3.11
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -e '.[test]'

# 重新生成固定样本
.venv/bin/python -m table_features generate \
  --output data/samples.jsonl --seed 20240517 --rows 24

# 正常运行
.venv/bin/python -m table_features run \
  --samples data/samples.jsonl \
  --config config/features.json \
  --cache-dir .cache \
  --snapshot-dir .snapshots \
  --output-dir runs/normal \
  --clear

# 四阶段注入诊断
.venv/bin/python -m table_features run \
  --samples data/samples.jsonl \
  --config config/features.json \
  --injection config/injections.json \
  --cache-dir .cache-injected \
  --snapshot-dir .snapshots-injected \
  --output-dir runs/injected \
  --clear

# 查询某特征对应的缓存键组成与内容哈希
.venv/bin/python -m table_features inspect-cache \
  --cache-dir .cache --snapshot-dir .snapshots --feature color

# 全量测试与验收演示
.venv/bin/python -m pytest
.venv/bin/python scripts/run_demo.py
```

`scripts/run_demo.py` 会生成 `artifacts/demo/demo_report.json` 与 `artifacts/demo/verification.md`，并保留正常运行、注入运行、增量运行及快照探测的产物。

## 项目结构

```text
src/table_features/
  config.py       配置解析与字段级校验
  data.py         固定种子 JSONL 样本生成与投影摘要
  injection.py    四阶段参数驱动注入
  stages.py       编码、标准化、交叉、选择及降级
  cache.py        内容寻址缓存与按特征查询
  pipeline.py     编排、快照提交、增量失效与产物写出
  snapshots.py    配置/统计量/选择快照版本化
  cli.py          命令行入口
config/           正常配置、四阶段注入、未归因异常注入
data/             固定种子样本
tests/            35 项本地测试
artifacts/demo/   实际运行证据
```

## 配置口径

`config/features.json` 中：

- `category_features[].encoding`：`one-hot` 或 `target`。
- 目标编码字段：`smoothing`、`folds`、`seed`。
- `numeric_features[].standardization`：`z-score` 或 `min-max`。
- `cross_slots[]`：二维或三维类别组合，显式给出 `representation`、`column_prefix`、`name_separator`、`value_separator`。
- `selector.type`：`variance` 或 `mutual_info`；`mode` 为 `top_k` 或 `threshold`。
- 三个独立快照版本：`config_version`、`statistics_version`、`selection_version`。

实现规则：

- One-hot 列名：`cat__<feature>__<urlquote(value)>`，词表按类别文本升序。
- 目标编码：先按配置种子生成行索引置换，再以 `position_in_permutation % folds` 分折；每行使用其它折计算
  `(sum_y_other_folds + smoothing * global_mean_other_folds) / (count_other_folds + smoothing)`。
  折大小、逐行折号、平滑系数、公式和逐行折外全局均值均写入统计结果。
- Z-score：`(x - mean) / std`，`ddof=0`；常量列标准化为 0。
- Min-max：`(x - min) / (max - min)`；常量列映射为 0。
- 交叉列只依赖原始类别值，不依赖基础类别列的编码方式。One-hot 交叉列名为
  `<prefix><sep><slot_id><sep><urlquote(v1)><value_sep>...`；ordinal 表示只生成一列，编码按组合排序。
- 交叉结果记录列数、列名和 `rows * columns * 8 bytes` 的 dense float64 内存估算。
- 选择排序：分数降序，同分按特征名升序。方差使用总体方差 `ddof=0`；互信息使用等频分位箱与自然对数公式。
- 保留清单始终是当前编码矩阵列的子集。

## 缓存与增量

对象文件位于 `<cache>/objects/<content_hash>.json`，索引位于 `<cache>/index.json`。缓存键由 canonical JSON 的 SHA-256 构成，包含：

- `sample_content_digest`：特征依赖列的投影摘要，始终含 `row_id` 和行顺序；
- `config_version`、`statistics_version`、`seed`；
- `slots_digest` 与 `slots_digest_scope`：按依赖范围绑定。基础编码/标准化使用“无交叉依赖”的空集摘要，单个交叉槽使用自身定义摘要，选择使用全量槽位摘要；
- 阶段、特征、特征配置摘要、注入配置摘要；选择另有矩阵版本与选择器摘要。

这样数值列变更不会污染类别编码和交叉键；类别编码方式变更不会污染基于原始类别的交叉键；增加无关槽位不会污染基础编码键。任何 `config_version` 提升都会保守地使全部缓存键变化，优先保证版本边界正确。

增量入口输出两部分：

- `plan`：受影响阶段、受影响特征、重算特征、复用特征与失效原因；
- `observed`：本次实际缓存命中/重算、重算阶段、重算列和复用列。

报告会断言 `recompute_reuse_disjoint=true`。未受影响的上游快照直接复用，只有受影响后缀进入重算。

## 异常注入与降级

注入完全由输入 JSON 驱动，不修改源码。支持：

- `encode`：`category_value_out_of_range`、`target_folds_non_positive`；
- `standardize`：`non_finite_numeric`、`numeric_value_out_of_range`；
- `cross`：`missing_slot_column`、`duplicate_slot`；
- `select`：`threshold_out_of_bounds`、`bins_invalid`；
- 任意阶段：`unclassified_exception`，用于验证顶层 `E_UNCLASSIFIED` 汇总。

每条错误记录含 `stage`、`code`、`feature`、`row_id`、`message` 和 `details`。降级链示例：

| 触发 | 降级 |
| --- | --- |
| 类别编码失败 | 跳过失败编码特征；依赖该特征的交叉槽跳过 |
| 数值标准化失败 | 跳过失败数值特征，其它编码不变 |
| 单个交叉槽无效 | 仅跳过该槽，保留基础特征和其它槽 |
| 选择参数注入失败 | 回退到默认方差阈值选择 |
| 目标特征数超过可用数 | 回退到全部可用特征 |
| 未归因异常 | 汇总 `E_UNCLASSIFIED`；选择回退默认方差 |

注入样例见 `config/injections.json` 与 `config/injections_unclassified.json`。业务码与字段级校验：

- `E_CONFIG_INVALID`：未知编码/标准化、非法折数/阈值/分箱数、注入的参数错误；
- `E_SLOT_REF_INVALID`：槽位引用缺失或非类别列；
- `E_SLOT_DUP`：槽位 ID 或组合重复；
- `E_SELECT_TARGET_INVALID`：目标特征数超过可用数；
- `E_CACHE_CORRUPT`：缓存对象、索引键组成或内容哈希损坏；
- `E_SNAPSHOT_ROLLBACK` / `E_SNAPSHOT_CORRUPT`：快照回退或校验失败；
- `E_UNCLASSIFIED`：未被阶段归因的异常。

## 快照

`SnapshotStore` 为 `config`、`statistics`、`selection` 各保存一个完整 envelope：

```json
{
  "format_version": 1,
  "kind": "statistics",
  "version": 1,
  "checksum": "<sha256 of canonical payload>",
  "payload": {}
}
```

写入使用临时文件与原子替换。版本号提升时整体替换；同版本校验和变化也整体替换；低版本尝试返回 `E_SNAPSHOT_ROLLBACK`；JSON 或 checksum 损坏返回 `E_SNAPSHOT_CORRUPT`。

## 验证

```bash
.venv/bin/python -m pytest
```

当前结果：`35 passed`。实际验收摘要见 `VERIFICATION.md`，机器可读证据见 `artifacts/demo/demo_report.json`。

目标编码当前面向全量训练批次做折外统计；不包含单独的服务端在线推断状态。该边界不影响本项目的编码、选择、缓存、失效与诊断验收。
