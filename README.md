# 表格数据线性模型正则化实验台

这是一个完全本地、无容器、无 GPU 的确定性实验台，用于验证 L2 岭回归、L1 套索和弹性网络线性模型在正则化方案频繁变更、并发批次计算、快照恢复与配置失效场景下的一致性。输入是 YAML 实验配置和 YAML 数据描述；每次运行输出可审计 JSON 报告、逐轮快照、已验证结果缓存和交互决策日志。

## 快速开始

环境要求：Python 3.10+、NumPy、PyYAML。

```bash
python3 -m pip install -r requirements.txt

# 15+ 项单元/集成测试，包括真实 SIGTERM 与 SIGKILL 子进程测试
python3 -m unittest discover -s tests -v

# 自动验收：三种正则化、单线程/4线程、缓存重放、快照恢复、配置失效
python3 -m regularization_lab verify \
  --config configs/experiment.yaml \
  --output reports/verification.json

# 默认训练配置中的全部方案，报告写入 reports/latest.json
python3 -m regularization_lab run --config configs/experiment.yaml

# 可重复交互命令序列
python3 -m regularization_lab run \
  --config configs/experiment.yaml \
  --state-dir .lab_state_demo \
  --report reports/demo_report.json \
  --commands commands/demo_commands.txt

# 或进入 REPL
python3 -m regularization_lab interactive --config configs/experiment.yaml
```

也可以直接运行：

```bash
bash scripts/run_demo.sh
bash scripts/run_acceptance.sh
```

## 工作区内容

- `configs/experiment.yaml`：优化器、划分、三种正则化方案、并发数与快照策略。
- `configs/data.yaml`：固定种子、320 行合成表，含数值列、类别列、缺失值、冗余列和目标列。
- `commands/demo_commands.txt`：反复切换方案、修改惩罚系数、复用缓存的可重复命令序列。
- `regularization_lab/config.py`：配置解析和逐项业务校验。
- `regularization_lab/data.py`：确定性数据生成、缺失填充、类别编码、固定划分和数据指纹。
- `regularization_lab/objective.py`：L2/L1/弹性网络显式损失与梯度。
- `regularization_lab/trainer.py`：线程池批次计算、固定顺序聚合、逐轮快照。
- `regularization_lab/snapshot.py`：原子快照、最新指针、进度和恢复校验。
- `regularization_lab/cache.py`：已收敛结果缓存与复用/失效判定。
- `regularization_lab/numeric.py`：数值参考和 `E_NUMERIC_DRIFT` 检查。
- `regularization_lab/lab.py`：会话编排、交互状态、决策日志和原子报告。
- `regularization_lab/cli.py`：`run`、`interactive`、`verify` 入口。
- `reports/`：已生成的验收报告、演示报告和完整运行报告。

## 数据与预处理

数据描述中的列按声明顺序使用 `numpy.random.default_rng(seed)` 再生。数值列支持正态/均匀分布；类别列使用声明概率；冗余列从指定数值源确定性生成；目标列使用固定顺序的偏置、数值系数、类别效应和固定噪声合成。

- 数值缺失以训练前的列中位数填充。
- 类别缺失以众数填充；并始终编码显式的 `__MISSING__` 类别。
- 类别按字典序独热编码，特征顺序由配置中的 `feature_columns` 固定。
- 数值特征使用训练集均值/总体标准差做确定性标准化。
- 数据先按 `split.seed` 固定置换，再按 `train_ratio` 划分。
- `data_fingerprint` 是最终训练/验证矩阵、目标、特征名和划分索引的 SHA-256。

未知特征列、未知公式/冗余引用、类型不符、目标列缺失、负惩罚系数、非正并发数等都会生成 `E_CONFIG_INVALID` 失败项。无效方案被跳过，非法并发数降级为 1，其他有效方案仍继续训练并写报告。

## 目标函数与确定性优化

设权重为 `w`，截距不参与正则化，数据项为 MSE：

```text
J_data = mean((y - (Xw + b))^2)
J_ridge = J_data + alpha * 0.5 * sum(w^2)
J_lasso = J_data + alpha * sum(abs(w))
J_elastic = J_data + alpha * (rho * sum(abs(w)) + (1-rho) * 0.5 * sum(w^2))
```

L1 次梯度使用固定规则：`sign(w) = 1`（`w>0`）、`-1`（`w<0`）、`0`（`w=0`）。弹性网络的两个正则分量分别记录在每轮的 `regularization_components` 中。

优化器为确定性全量梯度下降：每轮按固定行顺序划分固定批次，线程池只负责各批次的数据损失和梯度贡献；主线程按稳定键 `(round, batch_index)` 排序后，以固定批次顺序串行累加。逐轮记录起点系数处的数据损失、正则损失、总损失、系数增量、截距增量和更新范数。连续两轮总损失差严格小于配置容差时判定收敛。默认配置声明容差为 `1e-3`。

## 快照、恢复与版本

每完成一轮，训练器原子写入一个完整快照，包含当前系数、轮次、完整损失轨迹、优化器状态、配置哈希、数据指纹和校验和。写盘采用同目录临时文件、`fsync`、`os.replace` 和目录 `fsync`。`snapshot_commit_interval` 控制最新指针与进度文件的提交节拍；首轮、完成轮、中断轮总会提交。即使指针落后，启动恢复也会扫描当前配置哈希目录中版本最高且校验通过的完整快照。

配置哈希包含正则化方案类型、名称、惩罚系数、混合比例、优化器参数和再生数据指纹；不包含并发度、快照节拍、延迟或状态路径，因此并发度变化不破坏可恢复性。恢复时校验快照 schema 版本、配置 schema 版本、配置哈希、特征顺序和状态校验和。

如果启动时最新持久化状态属于其他正则化配置，会话记录 `E_CONFIG_MISMATCH` 并拒绝恢复旧快照，随后可用新配置从零训练。恢复过程不会把旧轮次伪装成新配置结果。

## 缓存复用与失效

缓存键由以下四项的稳定规范 JSON 派生：

```text
方案名 + 惩罚系数 + 混合比例 + 数据指纹
```

缓存条目还保存完整训练指纹；只有状态为 `complete` 且 `converged=true` 的结果能被复用。精确重放时直接返回已验证系数和完整轨迹，不执行训练、不重复写轮次快照。惩罚系数或方案变化会产生新缓存键，报告中的 `reused_rounds` 与 `recomputed_rounds` 明确列出复用/重算范围；从快照恢复的已完成前缀另列为 `restored_rounds`。改回已缓存的配置可立即复用原结果。

## 并发一致性与数值保护

每个批次任务携带 `(round, batch_index)` 稳定排序键。无论线程完成顺序如何，损失、梯度、失败清单和最终报告都按固定顺序聚合。线程池不设置超时，也不会把超时描述成训练已终止。

同一训练指纹首次完整完成后会写入数值参考。后续完整计算/恢复结果会与参考逐轮损失和最终系数比较；配置中的 `numeric_tolerance` 默认 `1e-12`，绝对差超限时记录 `E_NUMERIC_DRIFT` 并写入报告。`verify` 还会独立比较并发 1 与并发 4 的报告，本次实测逐字段完全一致，最大差为 0。

## 信号与异常语义

- `SIGINT`/`SIGTERM`：设置停止标志，完成当前轮计算、快照和进度提交，写报告并以退出码 0 退出。
- `SIGKILL`：无法处理，不承诺生成退出报告；重启只使用已完整原子替换的快照。本次集成测试在随机轮次强杀后成功恢复，最终系数与轨迹和不中断运行逐字段一致。
- 单批次目标/梯度异常：记录轮次、批次序号、阶段、业务码和异常类型，继续其他批次与后续轮次。
- 正则目标/参数更新异常：记录 `E_UPDATE_FAILURE`，该轮使用零增量继续。
- 快照写异常：记录 `E_SNAPSHOT_FAILURE`，内存训练继续；后续轮次仍可重新建立快照。

业务码包括：`E_CONFIG_INVALID`、`E_CONFIG_MISMATCH`、`E_NUMERIC_DRIFT`、`E_BATCH_FAILURE`、`E_UPDATE_FAILURE`、`E_SNAPSHOT_FAILURE`、`E_SNAPSHOT_INVALID`、`E_CACHE_INVALID`、`E_COMMAND_INVALID`。

## 报告

报告通过临时文件加原子替换写入。主要字段：

- `schemes`：各方案最终系数、完整逐轮损失/正则分量/更新、验证 MSE、快照版本。
- `reuse_and_recompute` 与 `cache_decisions`：缓存键、命中原因、复用轮、重算轮、恢复前缀。
- `decision_log`：无时间戳的操作序列，便于逐字段比较。
- `snapshot_versions`、`latest_snapshot_pointer`：快照版本与恢复点。
- `failures`、`business_codes`、`business_code_counts`：稳定排序的失败清单。
- `runtime_policy`、`exit_semantics`：快照策略、数值容差、信号语义。

## 演示命令与预期观测

`commands/demo_commands.txt` 依次执行 ridge、lasso、elastic，将 elastic 惩罚从 `0.06` 改为 `0.07` 再改回 `0.06`，最后重放 ridge。

预期：

1. 首次 ridge/lasso/elastic 训练均为缓存未命中。
2. elastic `alpha=0.07` 产生新缓存键并完整重算。
3. 改回 `alpha=0.06` 时命中已验证缓存，`recomputed_rounds=[]`。
4. 重放 ridge 命中缓存，不执行训练。
5. 修改 elastic 惩罚后旧快照会触发一次 `E_CONFIG_MISMATCH`，表示拒绝把旧状态恢复到新版本；这不是进程崩溃。
6. `reports/demo_report.json` 的 `decision_log` 可审计所有方案切换和惩罚修改。

本次实际运行的复用结果：

```text
ridge    cache_miss -> 96 rounds; replay cache hit, 96 reused, 0 recomputed
lasso    cache_miss -> 111 rounds
elastic  cache_miss -> 112 rounds; alpha=0.07 -> 114 rounds
elastic  alpha=0.06 replay cache hit, 112 reused, 0 recomputed
ridge    replay cache hit, 96 reused, 0 recomputed
```

## 本次验收结果

`reports/verification.json` 由 `verify` 直接生成，`reports/latest.json` 是全方案完整运行报告。

| 检查 | 结果 |
| --- | --- |
| 数据确定性 | 通过，指纹 `179a33850e586e74f574375c8cfd59a5657f7ba179a14aa6dba820124bfa94d2` |
| L2 / L1 / 弹性网络 | 全部收敛，分别为 96 / 111 / 112 轮 |
| 串行 vs 并发 4 | 报告逐字段一致；逐轮损失和最终系数最大绝对差均为 `0.0` |
| 缓存重放 | 三方案均 `cache_hit=true`，重算轮为空 |
| 第 40 轮快照恢复 | 从第 40 轮恢复；系数与完整损失轨迹相对不中断运行完全一致 |
| 修改 ridge `alpha` | 产生 `E_CONFIG_MISMATCH`，旧快照恢复载荷为空 |
| SIGTERM | 退出码 0，报告状态 `interrupted`，重启后与不中断运行完全一致 |
| SIGKILL | 无退出报告保证；重启使用最新完整快照，结果与不中断运行完全一致 |
| 测试套件 | `python3 -m unittest discover -s tests -v` 全部通过 |

三种方案最终指标（标准化后特征空间）：

| 方案 | 轮次 | 数据项损失 | 正则项损失 | 验证 MSE |
| --- | ---: | ---: | ---: | ---: |
| ridge | 96 | 0.3371204845 | 0.1453558974 | 0.3110091511 |
| lasso | 111 | 0.3244594693 | 0.2417472268 | 0.3005664253 |
| elastic | 112 | 0.3361695836 | 0.2202516241 | 0.3072519156 |

注意：损失记录取每轮更新前的参数，更新应用后立即写快照；这一约定是实现恢复逐字段一致的关键。
