# 谱图混合专家离线工作台

这是一个面向拉曼/X 射线衍射谱图预训练的纯 NumPy 本地工作台。它从固定种子生成三套格式版本，完成统一 token 化、top-k 门控、容量约束、负载均衡、塌缩检测与调整、三版本一致性审计、独立参考路由核对，并导出可继续微调的参数快照。

整个流程不访问网络，不使用外部数据集。

## 环境与启动

要求 Python 3.10+，唯一第三方依赖为 NumPy：

```bash
python3.10 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

默认命令：

```bash
python run.py \
  --seed 20261128 \
  --config configs/default.json \
  --output-dir outputs/default
```

输出目录非空时需显式加 `--overwrite`：

```bash
python run.py --seed 20261128 \
  --config configs/default.json \
  --output-dir outputs/default \
  --overwrite
```

安装为本地命令也是可选的：

```bash
python -m pip install -e .
spectral-workbench --seed 20261128 --config configs/default.json \
  --output-dir outputs/default
```

## 实现口径

### 三版本谱图

生成器在统一的采集段索引 `0..N-1` 上产生同一材料、同一仪器来源的基础谱峰，再写出三个版本：

| 版本 | 网格 | 强度归一 | 缺失标记 |
| --- | --- | --- | --- |
| A | 波数 `cm^-1` | 最大强度 | `NaN` |
| B | 波长 `nm` | 峰面积（缺失段按连续有效段积分） | 负值 `-1` |
| C | 角度 `deg` | 有效强度 90% 分位数 | 空字符串 |

解析器只按 `token_index` 对齐，不做跨单位重采样，因此三个版本中相同材料的相同谱段始终落在同一 token 索引。缺失 token 的掩码为 `0`。

各版本先执行自己的归一化，再对有效 token 执行零均值化和 L2 归一。由于生成时三个版本的原始强度是同一基础谱的标量倍，最大值、积分和分位数归一化仍然是标量倍，最终统一 token 向量在三个版本中逐元素一致。生成器、解析器和归一化日志保留了完整可追溯信息。

### Token 特征

每个归一 token 仍保留为标量强度，并附带二值掩码。门控前使用确定性的固定特征映射：

- 强度内容特征：`x`、`|x|`、`x^2`、`x|x|`；
- 共享位置特征：不同频率的正弦/余弦编码；
- 特征总维度由 `model.token_feature_dim` 指定，默认 `16`；
- 缺失 token 的特征全为 `0`，且不参与门控或负载统计。

该映射没有随机参数，三版本和参考计算器共享同一索引语义。

### 路由、容量与损失

门控为线性亲和度加温度缩放 softmax：

```text
logits = token_features @ gate_weight + gate_bias
probs  = softmax(logits / temperature)
```

每个有效 token 按概率稳定排序取 top-k。候选分配按 `(-probability, token, rank, expert)` 确定顺序处理，容量为：

```text
capacity = floor(capacity_factor * active_tokens * top_k / num_experts)
```

超出容量的动作由 `model.overflow_strategy` 决定：

- `drop`：丢弃该次分配；
- `reroute`：在未达容量且尚未选中的专家中按门控概率重路由；没有可用专家时丢弃。

最终分配矩阵和归一化激活权重都会反映容量处理结果。损失口径如下：

```text
balance_loss  = num_experts * dot(
                    final_token_count_share,
                    final_activation_weight_share)
route_loss    = mean(logsumexp(logits / temperature)^2)
capacity_loss = overflow_events / (active_tokens * top_k)
total_loss    = task_loss
              + balance_weight  * balance_loss
              + route_weight    * route_loss
              + capacity_weight * capacity_loss
```

`task_loss` 是门控加权专家线性输出的均方重构损失。门控和专家参数由固定种子初始化，使用 Adam 每步更新。每个训练步记录的是该步前向所用门控快照，因此参考计算器可以精确重放。

### 塌缩检测与调整

专家利用率为：

```text
utilization[e] = final_token_count[e] / total_final_assignments
```

当某专家连续 `collapse.patience_steps` 步低于 `collapse.utilization_threshold` 时，写入包含步号、专家编号、连续步数、利用率和承载数的告警。满足冷却步数后按 `collapse.adjustment_strategy` 自动调整：

- `temperature`：将温度乘以 `temperature_multiplier`，不超过 `max_temperature`；
- `reinitialize_gate`：用配置种子偏移重新初始化门控，并清空门控 Adam 状态；
- `none`：只告警，不调整。

每次调整都记录同一批样本在调整前后的完整逐 token 分配矩阵、激活权重、专家承载数、利用率、容量统计和损失差值，同时保存独立 `.npz` 数组。

## 输出结构

```text
outputs/default/
├── run_summary.json
├── resolved_config.json
├── generated_data/
│   ├── manifest.json
│   ├── version_A/*.csv
│   ├── version_B/*.csv
│   └── version_C/*.csv
├── normalization/
│   ├── normalization_log.jsonl
│   ├── index_alignment.json
│   ├── version_A.npz
│   ├── version_B.npz
│   └── version_C.npz
├── training/
│   ├── step_records.jsonl
│   ├── assignment_records.jsonl
│   ├── step_arrays.npz
│   ├── collapse_alerts.jsonl
│   ├── adjustments.json
│   ├── adjustments/*.npz
│   └── training_summary.json
├── reports/
│   ├── reference_check.json
│   ├── cross_version_check.json
│   ├── cross_version_arrays.npz
│   ├── specialization_report.json
│   ├── specialization_arrays.npz
│   └── acceptance_checks.json
└── snapshot/
    ├── model_snapshot.npz
    └── snapshot_manifest.json
```

关键审计信息：

- `step_records.jsonl`：每步样本、温度、专家承载数、样本数、利用率、容量溢出、丢弃/重路由数、近亲和 token、容量受影响 token 和全部损失。
- `assignment_records.jsonl`：每步归一标量 token、掩码、完整 `[batch, token, expert]` 分配矩阵和激活权重。
- `step_arrays.npz`：供独立参考计算器高效重放的 token 特征、归一标量 token、掩码、门控快照、概率、分配、权重和损失。
- `cross_version_check.json`：同批 A/B/C 的承载数、均衡损失和逐 token 差异清单。原因分类包括 `mask_mismatch`、`normalization_value_mismatch`、`assignment_mismatch`、`activation_weight_mismatch`、`gate_probability_mismatch`、`load_count_mismatch` 和 `balance_loss_mismatch`。
- `specialization_report.json`：按材料体系、仪器来源及二者组合输出主专家 token 占比、最终分配占比、激活权重均值，并标记稀有组单一专家占比超过 `evaluation.concentration_threshold`（默认 80%）。
- `reference_check.json`：独立固定规则路由器的逐步核对结果；不一致时给出首个不一致步及该步门控、概率、top-k、容量和 token 级依据。

## 场景配置

```bash
# 容量压力：小容量因子，产生大量 drop/reroute
python run.py --seed 20261128 --config configs/capacity_stress.json \
  --output-dir outputs/capacity_stress --overwrite

# 温度调整：强制单专家集中，持续低利用率后提高路由温度
python run.py --seed 20261128 --config configs/collapse_temperature.json \
  --output-dir outputs/collapse_temperature --overwrite

# 门控重初始化：持续低利用率后确定性重置门控
python run.py --seed 20261128 --config configs/collapse_reinit.json \
  --output-dir outputs/collapse_reinit --overwrite

# 近亲和度：高温使 top-k 概率接近，记录近亲和 token
python run.py --seed 20261128 --config configs/near_tie.json \
  --output-dir outputs/near_tie --overwrite
```

`collapse_temperature.json` 同时用于稀有材料集中告警的端到端验收：其固定门控偏置会使主专家集中度超过 80%，从而在专精报告中产生 `rare_concentration_warning`。

## 独立参考计算器

`spectral_workbench/reference.py` 不导入训练使用的 `routing.py`，而是在归一 token 特征、保存的门控快照和容量配置上独立实现同一固定规则，逐步比较：

- 完整分配矩阵；
- 激活权重；
- 每专家 token 承载数；
- 每专家样本数；
- 容量因子；
- 负载均衡损失。

默认流水线自动生成 `reports/reference_check.json`。也可在代码中单独调用：

```python
from spectral_workbench.reference import verify_training_records
from spectral_workbench.config import load_config

report = verify_training_records(
    "outputs/default/training/step_arrays.npz",
    load_config("configs/default.json"),
)
assert report["verified"]
```

## 参数快照

`snapshot/model_snapshot.npz` 包含：

- `gate_weight`：`[token_feature_dim, num_experts]`
- `gate_bias`：`[num_experts]`
- `expert_weight`：`[num_experts, token_feature_dim, token_feature_dim]`
- `expert_bias`：`[num_experts, token_feature_dim]`
- `temperature`
- 随机种子、配置指纹、专家数、top-k 和 token 维度

下游可以按相同 `build_token_features` 规则构造输入，加载这些参数继续微调。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖：

- A/B/C 三种缺失标记解析、索引对齐与规范化 token 一致性；
- top-k 容量丢弃和溢出重路由、容量上限及近亲和 tie-break；
- 端到端参考核对、跨版本一致性和固定种子复现；
- 篡改一步分配后，参考核对器定位首个不一致步并输出路由依据；
- 温度调整前后的分配、利用率和损失对比；
- 门控重初始化及稀有材料单一专家超过 80% 的告警。

## 复现性

同一 `seed`、配置和 NumPy 环境下：

- 原始谱图 CSV、归一化日志和训练记录完全一致；
- 门控初始化、样本批次顺序、容量调度和调整种子均确定；
- `step_records.jsonl`、`normalization_log.jsonl` 和所有报告可直接逐行比较。
