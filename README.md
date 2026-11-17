# 表格集成训练诊断工作台

这是一个无网络依赖、可在 Windows 原生 Python 3.11 上运行的确定性表格集成训练工作台。它读取本地 YAML 配置与列描述，生成合成二分类数据，完成稳定等频分箱、带行/列子采样的残差树训练、固定树序集成打分、路径特征贡献、浮点误差逐级追踪、阶段快照恢复和增量复用审计。

项目不使用 Docker、容器、镜像、在线账号或外部数据服务。`scikit-learn` 只用于报告中的可选基线对照，核心数据生成、分箱、训练、打分、贡献和诊断均不依赖 scikit-learn。

## 快速开始

Linux/macOS：

```bash
python3.11 -m pip install -r requirements.txt
python3.11 -m gbdt_workbench validate --config configs/example.yaml
python3.11 -m gbdt_workbench run --config configs/example.yaml --output reports/demo
```

Windows PowerShell：

```powershell
py -3.11 -m pip install -r requirements.txt
py -3.11 -m gbdt_workbench validate --config configs/example.yaml
py -3.11 -m gbdt_workbench run --config configs/example.yaml --output reports\demo
```

全量重建使用独立输出目录：

```bash
python3 -m gbdt_workbench rebuild --config configs/example.yaml --output reports/full
python3 -m gbdt_workbench compare \
  --left reports/demo/diagnostic_report.json \
  --right reports/full/diagnostic_report.json
```

`rebuild` 会忽略已有阶段缓存并重新计算全部分箱、树、评分和贡献。增量运行直接使用同一 `--output` 目录即可。

## 交付物

```text
gbdt_workbench/        核心包与 CLI
configs/               正常配置、数据描述、float64 与异常注入样例
configs/anomalies/     非法学习率、类型错误、未知列、目标缺失、单树故障样例
reports/               运行报告与快照输出目录
tests/                 数据、分箱、树、精度、增量、并发、快照测试
scripts/               预留本地脚本目录
```

运行后主要文件：

- `diagnostic_report.json`：确定性诊断报告，是增量/全量等价性的契约。
- `run_report.json`：完整运行报告，包含确定性诊断内容以及复用清单、线程信息、耗时和快照提交审计。
- `snapshots/`：分箱、树、集成、贡献、诊断的原子 JSON 快照。
- `snapshots/manifest.json`：快照版本、配置指纹、阶段键、树键和提交计数。

`run_report.json` 中的运行路径、耗时、恢复状态和复用动作天然依赖运行历史，因此不参与等价性比较。`diagnostic_report.json` 不包含这些操作元数据，并从空白工作区全量重建产生相同字段值和 `equivalence_digest`。测试会直接比较两个 `diagnostic_report.json` 对象。

## 配置格式

实验配置示例见 `configs/example.yaml`：

```yaml
schema_version: 1
seed: 20261117
data_spec: data_spec.yaml
experiment:
  n_trees: 10
  max_depth: 3
  learning_rate: 0.12
  row_subsample: 0.82
  col_subsample: 0.75
  min_samples_leaf: 5
  n_bins: 12
  precision: float32       # float32 | float64
  tolerance: 1.0e-6
runtime:
  n_jobs: 4
  snapshot_interval: 2
diagnostics:
  decision_threshold: 0.5
  include_baseline: true
  fault_tree_indices: []
```

列描述支持：

- `numeric`：均值、标准差、可选上下界、缺失率、目标效应系数。
- `categorical`：类别、可选采样权重与缺失率、类别到目标效应的映射。
- `redundant`：复制已有列，可选数值噪声；用于冗余列诊断。
- `target`：二分类目标名、正类基准比例、噪声标准差和可选截距。

所有随机流都由 `seed + 用途 + 列名/树序号` 经 SHA-256 派生，因此同一解析后配置和种子会生成逐元素一致的数据。

## 模型语义与确定性规则

该工作台采用可并发的“随机残差树集成”：

1. 初始原始得分是目标正类比例的对数几率，所有树独立拟合同一初始逻辑残差 `y - p0`。因此各树可并发训练，同时保留残差叶子值、行/列子采样和学习率累加。
2. 每棵树训练时用固定树序号派生随机流，独立抽取行子集和列子集。
3. 节点分裂使用显式顺序累加的方差下降增益；只在左右子节点均满足 `min_samples_leaf` 时成为候选。
4. 并列增益保留先遇到者：先按特征索引升序，再按分箱阈值升序。
5. 叶子值是按训练样本行顺序显式累加后的残差均值。
6. 原始集成分数是 `base_logit + learning_rate * sum(leaf_value)`，树严格按树序号累加；概率使用数值稳定的分段 sigmoid。
7. 每棵树的缩放叶子输出在路径分裂特征间等额分配；同一路径重复出现的特征按出现次数累计。数组始终按特征索引升序输出，因此同分和零贡献项顺序固定。无分裂的树没有可归因特征，其输出不进入特征贡献和。

该设计满足“独立树并发训练”和固定树序汇总。若改为标准逐树更新残差，第 `i` 棵树必须依赖前 `i-1` 棵树，无法满足任务要求的彼此独立树线程池。

### 分箱

- 数值列按请求的 `n_bins` 做等频分箱。
- 排序键是 `(值, 原始行号)`；并列值落在边界两侧时，固定由原始行号决定归属。
- `boundaries` 是从 `0` 到样本数的整数秩位置，不是浮点特征阈值。
- 类别列在确定性编码后使用类别码作为整数分箱。
- 分箱指纹包含数据指纹、分箱参数、边界、各箱计数和逐样本分箱矩阵。

## 精度与误差追踪

精度档位与参考档位：

| 配置档位 | 参考档位 | 用途 |
| --- | --- | --- |
| `float32` | `float64` | 完整链路参考计算 |
| `float64` | `decimal`（50 位上下文） | 标量求和、增益、缩放与集成交付的高精度参考 |

按固定顺序记录四类逐项差值：

1. `split_gain`：每个内部节点的分裂增益。
2. `leaf_value`：每个叶子的残差均值。
3. `learning_rate_scale`：每个叶子的学习率缩放结果。
4. `ensemble_sum`：每个样本的集成交付求和。

报告包含每项 `base`、`reference`、`abs_diff`、`exceeds_tolerance`，以及首个超差项和首个超差环节。若参考精度概率与配置精度概率位于判定阈值两侧，则 `threshold_sensitive_samples` 记录样本、两侧概率、方向和主导贡献特征；这类记录与误差环节归因分开输出。

## 缓存键、失效与复用

缓存键均是内容寻址 SHA-256：

- 分箱键：数据内容指纹 + 列模式 + `n_bins` + 并列规则。
- 树键：分箱指纹 + 初始残差指纹 + 树序号 + 深度 + 叶子最小样本数 + 行/列采样比例 + 精度 + 该树是否故障注入。
- 集成键：固定树序的树内容指纹 + 初始得分 + 学习率 + 精度。
- 贡献键：固定树序的树内容指纹 + 学习率 + 精度 + 特征顺序。

实际失效/复用行为：

| 编辑 | 分箱 | 已有树 | 新树 | 集成/贡献/诊断 |
| --- | --- | --- | --- | --- |
| 树数量增加 | 复用 | 复用 | 仅新增序号训练 | 重算 |
| 最大深度变化 | 复用 | 全部失效 | 全部重训 | 重算 |
| 学习率变化 | 复用 | 复用 | 无 | 重算 |
| 分箱数变化 | 失效 | 全部失效 | 全部重训 | 重算 |
| 精度档位变化 | 复用 | 全部失效 | 全部重训 | 重算 |
| 并发数或快照间隔变化 | 复用 | 复用 | 无 | 复用/仅诊断 |
| 阈值或容差变化 | 复用 | 复用 | 无 | 仅诊断重算 |

`run_report.json` 的 `operational_audit` 同时给出：

- 每个阶段和每棵树的 `reused`/`computed` 动作。
- 相对上一清单发生变化的键（`recompute_set`）。
- 完整的前后缓存键对照。
- 快照提交计数、线程配置和可用 CPU 数。

## 快照与恢复

- 每个阶段先写入同目录临时文件，`flush + fsync` 后使用 `os.replace` 原子替换，Windows 兼容。
- 快照版本为 `gbdt-workbench-snapshot-v1`。
- 快照恢复校验版本、阶段名、缓存键、配置指纹字段和载荷 SHA-256。
- 清单版本或载荷身份不一致返回 `E_CONFIG_MISMATCH`。
- `--strict-restore` 额外要求当前配置指纹与清单一致；普通增量运行允许配置参数变化，但每个缓存产物仍会验证其自身身份与载荷指纹。
- 每完成 `snapshot_interval` 棵新树提交一次清单，结束前再提交最终清单，因此中断后已落盘的树可直接恢复。

## 业务码与退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 完整成功 |
| `1` | 部分树失败，其余树、集成与报告继续生成 |
| `2` | 配置或数据生成失败 |
| `3` | 快照版本/配置指纹/恢复载荷不一致（`E_CONFIG_MISMATCH`） |
| `4` | 输出目录或内部执行失败 |

配置失败统一使用 `E_CONFIG_INVALID` 并写入失败清单，包括未知列、类型不符、目标列缺失、负学习率、非正树数量和深度等。单树异常使用 `E_TRAIN_TREE`，记录树序号、阶段和异常信息，不影响其他树完成。

## 本地验证结果

验证环境：Python 3.11.0rc1、NumPy 2.4.6、PyYAML 6.0.3、scikit-learn 1.9.1、Linux/WSL，16 个可用逻辑 CPU。

```text
$ python3 -m pytest -q
........................                                                 [100%]
24 passed in 8.02s
```

默认配置实际运行：

```text
status=ok, exit_code=0
equivalence_digest=8170c4324ff61166354e1d11c1c94e0703846c641b02408ad079bb4a80eafaa7
first_exceeding_stage=split_gain
split_gain.max_abs_diff=4.667078602160757e-05
tree_count=10, failure_count=0
```

对默认配置再次增量运行：分箱、10 棵树、集成和贡献均复用，`computed_tree_indices=[]`，诊断摘要保持上述 digest 不变。

默认增量目录与独立全量重建目录经 `gbdt-workbench compare` 比较返回 `equal=true`、退出码 `0`，两侧 digest 均为 `8170c432...eafaa7`。

`configs/threshold_sensitive.yaml` 把判定阈值放在同一概率的配置精度/参考精度结果之间，实际报告得到 7 个阈值敏感样本，主导贡献特征均为 `monthly_usage`。

`configs/anomalies/tree_failure.yaml` 注入第 2 棵树失败：退出码为 `1`，报告为 `partial_failure`，其余树 `[0,1,3,4,5]` 正常完成，失败清单记录 `E_TRAIN_TREE` 与树序号 2。

测试还覆盖：

- 同一配置/种子数据逐元素一致。
- 分箱指纹、整数边界、并列行号规则和重复运行一致。
- 增益并列时选择低特征索引。
- 分箱数、深度、学习率、树数量和并发度编辑序列后的增量结果与独立全量重建逐字段一致。
- `n_jobs=1` 与多线程运行的树、得分和贡献完全一致。
- 快照版本不匹配、严格恢复配置指纹不匹配均返回退出码 3。
- float32→float64 与 float64→decimal 两条精度参考链均可运行。

## 不访问外部资源

运行和测试只读取工作区内 YAML/JSON 文件并写入本地输出目录。scikit-learn 基线也只使用本地内存数据；网络不可用时不影响核心工作台。
