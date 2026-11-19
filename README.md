# 确定性多标签图像分类与异常注入实验

本项目从零实现一条完全本地、可重复的多标签图像分类链路，并围绕它提供：

- 固定种子生成的 RGB 样本与多标签真值；
- 预处理、固定权重投影、逐标签线性打分、独立门限判决；
- 可审计的 micro/macro F1、逐标签 precision/recall、hamming loss；
- 预处理、特征、打分、判决四阶段参数化异常注入与显式降级；
- 配置、投影参数、真值标注三类快照的版本化、校验和与原子全量替换；
- 双次运行完整输出哈希比对和跨阶段一致性自检；
- 样本数、特征维度和驻留矩阵内存上限；
- 正常、四点注入、资源超限降级三套可复现实测报告。

运行过程不访问网络、不使用 Docker、不依赖外部数据集，也不依赖系统时间或未声明随机状态。

## 环境

- Python 3.10+
- NumPy
- pytest

### Windows PowerShell

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest -q
.\scripts\run_demos.ps1
```

若 PowerShell 执行策略限制脚本，可直接逐条执行 README 中的 `python -m multilabel_lab.cli ...` 命令，或先把 `$env:PYTHON` 指向 `.venv\Scripts\python.exe`。

### Linux / WSL

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pytest -q
PYTHON=.venv/bin/python ./scripts/run_demos.sh
```

## 目录

```text
multilabel_lab/       核心实现与 CLI
tests/                22 项自动化测试
examples/             多点注入计划
scripts/              Linux/WSL 与 PowerShell 演示脚本
artifacts/snapshots/  实际运行生成的原子快照
artifacts/reports/    实际运行生成的 JSON 报告
```

## 分类链路

默认样本为 `12 x 12 x 3` 的 RGB 数组，受控标签集为
`cat, dog, vehicle, indoor, outdoor`。样本像素由配置种子与样本序号确定；
多标签真值和不确定标签由标注种子确定。样本、真值与投影参数均在运行时从快照重建。

固定特征维度：

```text
D = grid_rows * grid_cols * 3 * (mean?1:0 + std?1:0)
```

默认 `3 x 3` 网格、均值和标准差均启用，因此 `D = 54`。

计算过程：

```text
1. pixel -> clip[0,255] -> /255                         (preprocess)
2. block means + block stds -> raw_features (D)          (feature)
3. projected = raw_features @ P, P is fixed D x D        (feature projection)
4. scores = projected @ W + bias, W is fixed D x L       (score)
5. for each label: decision = scores[label] >= threshold (decision)
```

正常路径使用 `P` 投影；特征阶段注入失败时跳过投影，直接使用 `raw_features`，
而打分矩阵仍与原始特征维度对齐。所有矩阵来自带版本和校验和的投影快照。

## 确定性与哈希

每个阶段输出都计算规范化摘要：

- JSON 使用 UTF-8、`sort_keys=True`、紧凑分隔符，并拒绝 NaN/Infinity；
- 数组摘要包含 `dtype`、`shape` 和 C-order 小端字节；
- 标签、样本、注入和降级均按显式列表顺序处理；
- 报告不使用时间戳、绝对路径、进程 ID 或未声明随机数；
- 完整核心报告可直接序列化为字节并由 SHA-256 哈希。

验证器独立执行两次分类，比较核心报告的完整输出哈希，并额外检查：

- 四个阶段的样本标识列表完全相同；
- 每个标签的 `score >= applied_threshold` 可重新得到判决；
- 判决标签集合与标签顺序一一对应；
- 注入与降级记录可追溯到样本，降级触发码均有来源；
- 指标可由逐样本逐标签明细重新算出且完全一致。

偏差返回 `E_VERIFY_FAIL`，`first_deviation` 给出首个位置；例如：

```json
{
  "kind": "decision_not_reproducible",
  "location": "$.samples[1].stages.decision.decisions[0]",
  "sample_id": "sample-0001",
  "label": "cat"
}
```

字节级一致性已实测于：

- Python 3.12.13 + NumPy 2.5.5；
- Python 3.10.12 + NumPy 2.2.6。

两者生成的默认 `normal.json` 文件 SHA-256 均为
`9812c8f0cd346630c947e5dbf0dd12e7c6e248d848e4f346c8a85c3630bfceac`。
跨 CPU 或操作系统版本时，如需最强保证，仍应固定完整运行环境。

## 多标签与不确定标签

真值记录同时保存 `labels` 与 `uncertain_labels`，两者都按受控标签顺序排列。
不确定标签按配置策略处理，并写入 `metrics.uncertainty.strategy`：

| 策略 | 指标处理 | 单独统计 |
| --- | --- | --- |
| `ignore` | 该样本-标签单元从指标中排除 | 记录排除数 |
| `positive` | 有效真值强制为 1，参与指标 | 记录不确定数 |
| `separate` | 从主指标排除 | 记录不确定数和对应预测正例数 |

逐单元明细包含 `sample_id`、`label`、`truth`、`prediction`、`included`、
`uncertain` 和 `reason`。任何分数向量、门限向量、判决向量或标签顺序不一致均返回
`E_INCONSISTENT`，并携带样本标识与首个相关标签。

## 指标口径

- micro precision/recall/F1 由全体有效单元的 TP/FP/FN 汇总；
- macro F1 对有定义标签的 F1 求平均；
- 逐标签 precision/recall/F1 同时记录 TP/FP/FN/TN；
- hamming loss = `(FP + FN) / included_cells`；
- 不确定标签被排除的单元不进入上述分母。

零分母必须声明 `metrics.zero_division`：

| 值 | 行为 |
| --- | --- |
| `zero` | 返回 `0.0` |
| `one` | 返回 `1.0` |
| `skip` | 返回 `null`，并从 macro 分子/分母中排除 |
| `error` | 返回 `E_METRIC_UNDEFINED` 并指出标签 |

策略缺失或非法同样返回 `E_METRIC_UNDEFINED`，不会产生除零异常。

## 异常注入与降级

注入完全由 JSON 输入计划触发，不需要修改源码。支持类型：

| 阶段 | 注入类型 | 业务码 | 降级类别 | 降级动作 |
| --- | --- | --- | --- | --- |
| `preprocess` | `pixel_out_of_range` | `E_PIXEL_RANGE` | `DEGRADE_CLAMP_PIXELS` | 裁剪到合法像素范围后继续 |
| `feature` | `projection_shape_mismatch` | `E_PROJECTION_SHAPE` | `DEGRADE_RAW_BLOCK_STATS` | 跳过投影，回退原始分块统计 |
| `score` | `label_index_oob` | `E_LABEL_INDEX` | `DEGRADE_BIAS_ONLY_SCORE` | 回退固定偏置分数 |
| `decision` | `nonpositive_threshold` | `E_THRESHOLD` | `DEGRADE_DEFAULT_THRESHOLD` | 该标签回退默认正门限 |

每条实际命中的注入记录包含阶段、类型、业务码、样本标识、标签（适用时）和原始参数。
每条降级记录包含阶段、降级类别、触发码、样本标识和降级详情。
没有命中注入点时 `injection_records` 为空。

降级后指标仍在正常字段中计算，但 `metrics_path.mode` 为 `degraded`，
并列出全部降级类别和触发业务码；每个受影响样本也有独立降级路径标记。

未归因异常不会静默丢弃：流水线记录 `unattributed`、使用显式安全回退，
返回 `E_UNATTRIBUTED`，并将指标标记为降级路径。

## 快照版本化

`SnapshotStore` 把 `config`、`projection`、`annotations` 放在同一个
`snapshot.json` bundle 中。每个类别包含：

```json
{
  "category": "config",
  "version": 1,
  "checksum": "sha256(...)",
  "payload": {}
}
```

校验和覆盖 `category + version + payload` 的规范化 JSON。提交时：

1. 要求三类完整存在；
2. 要求各类别版本严格单调递增；
3. 在写盘前校验整个新 bundle；
4. 写固定临时文件并 `fsync`；
5. 使用 `os.replace` 一次替换整个旧快照；
6. 加载时先校验所有类别和校验和，绝不使用部分可解析数据。

损坏、缺失或校验和不符返回 `E_SNAPSHOT_CORRUPT`，详情含快照类别与版本。
同版本内容变化可由校验和识别；版本变化由版本号识别。加载结果带
`replaced`、`changed_categories` 与 `change_reasons`。

## 资源上限

`limits` 对三项资源设上限：

- `max_samples`
- `max_feature_dim`
- `max_resident_matrix_bytes`

对应策略：

| 资源 | 策略 | 降级行为 |
| --- | --- | --- |
| 样本数 | `error` / `truncate` | 截断到上限，`DEGRADE_SAMPLE_TRUNCATION` |
| 特征维度 | `error` / `truncate` | 一致截断原始特征、投影矩阵与打分权重，`DEGRADE_FEATURE_TRUNCATION` |
| 驻留矩阵内存 | `error` / `truncate_samples` | 按每样本估算字节数截断样本，`DEGRADE_MEMORY_SAMPLE_TRUNCATION` |

内存估算口径：

```text
bytes_per_sample = (H * W * C + 4 * effective_feature_dim + label_count) * 8
```

若策略为 `error`，返回 `E_RESOURCE_LIMIT`；若即使降级也无法容纳一个样本，也返回该业务码。
资源降级记录列出被移除样本的标识，验证器可据此追溯。

## 四个入口

CLI：

```bash
python -m multilabel_lab.cli classify --help
python -m multilabel_lab.cli metrics --help
python -m multilabel_lab.cli inject --help
python -m multilabel_lab.cli verify --help
```

Python API：

```python
from multilabel_lab import classify, metrics, inject, verify
```

四个 API 分别对应分类、指标、注入和验证。CLI 的 `classify`、`inject`、`verify`
都会先执行双次运行验证，再输出包含 `report` 与 `verification` 的 JSON 报告。
`metrics` 读取既有报告并使用明细单元独立重算指标，检查与存储值一致。

## 实际演示

执行：

```bash
PYTHON=.venv/bin/python ./scripts/run_demos.sh
```

本次实测结果：

| 场景 | 状态 | 核心报告 SHA-256 | 结果 |
| --- | --- | --- | --- |
| 正常运行 | `ok / OK` | `6cca774104b983c0a92cd8a1bcb2eb810adceab8e3a2b58d4e020a34798178cf` | micro F1 `0.32`，macro F1 `0.1454545455`，hamming `0.4594594595` |
| 四点注入 | `degraded / OK_DEGRADED` | `58d5dba7a393de7147409f5c92e328d7dd067f0c7d6deaf45271c3dc21840b91` | 4 条注入记录、4 条降级记录、4 个业务码 |
| 资源降级 | `degraded / OK_DEGRADED` | `455bd174342b95c3e94f14e890f4e71296c6a8579f456ee9089f8ca1e7356575` | 12 个标注样本降级为 5 个，指标来自降级路径 |

报告文件 SHA-256：

```text
9812c8f0cd346630c947e5dbf0dd12e7c6e248d848e4f346c8a85c3630bfceac  artifacts/reports/normal.json
6431eba0064c1c39766af030b5411fb8c62873fc118b082155b6c5aa2ecf8742  artifacts/reports/injection_multi.json
50716b9fa46d368027dac194cdd8346e1023e9172d7e339e7cc41a970dc91dfa  artifacts/reports/resource_degraded.json
9812c8f0cd346630c947e5dbf0dd12e7c6e248d848e4f346c8a85c3630bfceac  artifacts/reports/verification.json
0c91e2a6cfa10364f69923d704851273470d89f91c5083a375a8c46d0b18e518  artifacts/reports/metrics_recompute.json
```

连续执行两次演示后，上述五个报告文件哈希完全不变。

测试结果（Python 3.12 / NumPy 2.5.5 与 Python 3.10 / NumPy 2.2.6 均通过）：

```text
22 passed
```

覆盖同一快照双跑字节一致性、阶段审计、四阶段注入、降级归因、
未归因异常、`E_INCONSISTENT`、三种不确定标签策略、零支持口径、
快照全量替换/篡改/版本单调性、样本/特征/内存资源限制、验证器首偏差与 CLI 集成。

## 报告结构

核心分类报告主要字段：

```text
schema_version, status, result_code, business_codes
business_code_counts, config, snapshot
resource_limits, resource_events
injection_records, stage_errors, degradations, unattributed
stage_audit, stage_manifests
samples[*].stages.{preprocess,feature,score,decision}
samples[*].stages.score.values / decision.predicted_labels
metrics.{micro,macro_f1,per_label,hamming_loss,details,uncertainty}
metrics_path
```

CLI 外层报告：

```text
report          核心分类报告
verification    first_hash, second_hash, matched, checks, first_deviation
```
