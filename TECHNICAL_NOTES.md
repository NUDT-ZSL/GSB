# 梯度提升树诊断组件（gbtdiag）技术说明

## 1. 范围与依赖

- 纯 Python + 唯一第三方依赖 `numpy==1.26.4`，无 GPU、无联网、无外部数据文件。
- 目标运行环境为 Windows 64 位 + Python 3.11；当前验证环境为 Linux +
  Python 3.10，代码仅使用跨平台标准库与 numpy 公共 API，两套环境行为一致。
- 样本由固定种子在进程内确定性生成（`gbtdiag/data.py`）。
- 运行产物统一落在 `runs/`，入口通过 `prepare_run_dir` 在运行前清空目录，
  避免跨次污染；快照为 JSON，缓存为 `.npy`。

## 2. 目录结构与四个入口

| 模块 | 职责 |
| --- | --- |
| `gbtdiag/config.py` | 训练配置、字段校验、按名称派生的独立 RNG 流 |
| `gbtdiag/data.py` | 固定种子的数值/类别特征与回归目标生成 |
| `gbtdiag/binning.py` | 数值分位数分箱、类别固定编码、候选排序、越界检测 |
| `gbtdiag/gain.py` | 增益公式、候选扫描、严格/容差选择策略、比较记录 |
| `gbtdiag/leaf.py` | 叶权重 `-Σg/max(Σh, floor)`、下限/次正规保护 |
| `gbtdiag/tree.py` | 递归树结构、路由预测、确定性序列化 |
| `gbtdiag/boosting.py` | 逐轮采样、训练、累加更新、独立复算 |
| `gbtdiag/precision.py` | 精度量化、四种舍入模式、次正规数刷新 |
| `gbtdiag/error_analysis.py` | 五级误差传播、放大比、主导来源与越界定位 |
| `gbtdiag/injection.py` | 四阶段注入清单与内存态注入控制器 |
| `gbtdiag/verifier.py` | 双训哈希、采样集合、独立复算、归因追溯 |
| `gbtdiag/api.py` | 对外门面：`train` / `predict` / `inject_diagnose` / `validate` |

## 3. 确定性与可复现设计

### 3.1 RNG 流布局

所有随机性都来自 `TrainConfig.rng(name)`，由主种子与固定流编号派生
`SeedSequence`，流名称是契约的一部分：

| 流名称 | 用途 |
| --- | --- |
| `data` | 样本生成（数据模块另用独立子序列） |
| `row_sample` | 每轮行采样 |
| `col_sample` | 每轮列采样 |
| `injection` | 注入默认样本选择 |
| `verify` | 验证保留流 |

每轮行/列采样在训练前一次性物化为显式索引数组，并写入逐轮明细，
因此“采样序列可复现”是可审计的数据，而不是隐含状态。

### 3.2 分箱与平局打破

- 数值列：在去重取值上计算分位数阈值，再去重得到候选集合。
- 类别列：非负整数固定编码，候选为 `code <= t`（`t = 0..card-2`）。
- 全局候选顺序：先按特征索引升序，再按阈值升序
  （`binning.ordered_candidates`），等增益只保留先出现候选，保证选择稳定。
- 分箱结果仅使用 Python `float`/`list` 结构，经 `canonical_bytes`
  规范化后做 SHA-256，同输入同配置逐字节一致。

## 4. 增益与分裂选择

增益采用二阶公式：

```
Gain = (Σg_L)^2 / max(Σh_L, floor)
     + (Σg_R)^2 / max(Σh_R, floor)
     - (Σg_P)^2 / max(Σh_P, floor)
```

- 每次候选与“不分裂基线（增益 0）”及当前最优候选比较，均记录两个候选
  与差值（`gain.SplitDecision.comparisons`）。
- `strict`：要求 `best_gain > min_gain` 精确成立。
- `tolerance`：要求 `best_gain + gain_tol >= min_gain`，门限附近候选
  不会因重复运行的舍入差异翻转。
- 叶样本数受 `min_leaf_samples` 约束；等增益不替换当前最优，天然实现
  特征索引、阈值升序的平局打破。

## 5. 叶权重数值稳定性

- 分母保护：`effective_hess = max(Σh, hessian_floor)`，是否介入写入
  `LeafResult.floor_protected` 并进入逐轮报告。
- 非法下限（非有限或负数）抛 `E_CONFIG_INVALID`，带 `field` 与叶
  `node_path`。
- 四种舍入模式 `half_even / half_up / half_down / trunc`，`float32`
  下在可表示尾数上生效；次正规数可配置刷新为带符号零，并标记
  `subnormal_observed`。
- 注入的非有限 Hessian 被替换为 `(grad=0, hess=hessian_floor_default)`，
  记录 `E_NONFINITE_HESSIAN` 与 `default_floor_fallback` 降级。

## 6. 逐级误差传播与归因

以同输入、同采样的 `float64` 全精度单轮链路为参考真值，对五个检查点
逐轮比较：

1. `quantization`：量化后的梯度/Hessian 拼接向量；
2. `gain`：根节点最优增益、特征、阈值；
3. `leaf_division`：未缩放叶权重向量；
4. `lr_scale`：学习率缩放后的树贡献；
5. `accumulation`：加法累加后的轮预测。

每级输出绝对偏差（L2 范数）、相对偏差、逐行误差区间 `[min, max]`、
相对上一级的误差放大比；相对偏差最大者为该轮主导来源，再按轮聚合得到
全局主导级别。任一级超过 `stage_tolerances` 即产生
`E_TOLERANCE_EXCEEDED`，记录越界阶段、轮次、行索引、参考值、候选值
与偏差值。

## 7. 可复现验证器

`verifier.verify_reproducibility` 训练两次并校验：

- 整报告 SHA-256 一致；
- 逐轮起始/结束损失、树结构哈希、预测哈希一致；
- 逐轮行/列采样索引集合与顺序一致；
- 最终预测与每轮前缀预测都能由“初始预测 + 各树贡献 × 学习率”按训练
  相同量化规则独立复算一致；
- 容差越界记录可回溯到误差报告中的偏差明细。

首个失败写入 `first_mismatch`（轮次 + 环节），业务码 `E_INCONSISTENT`。

## 8. 四阶段注入与降级链路

注入通过 `InjectionSpec` 清单驱动，仅在内存副本上改数据/参数，不修改
源码。

| 阶段 | 注入类型 | 业务码 | 降级类别与动作 |
| --- | --- | --- | --- |
| binning | `category_oor` | `E_CATEGORY_OUT_OF_RANGE` | `row_quarantine`：越界行退出所有树拟合池，贡献恒为零 |
| gain | `duplicate_threshold` | `E_DUPLICATE_THRESHOLD` | `skip_round_split`：节点保持叶/整轮零贡献占位树，沿用上轮预测 |
| leaf | `nonfinite_hessian` | `E_NONFINITE_HESSIAN` | `default_floor_fallback`：替换非有限对并回退默认下限 |
| prediction | `negative_learning_rate` | `E_NEGATIVE_LEARNING_RATE` | `zero_lr_hold`：学习率钳为零，保持上一轮预测 |

另有 `raw_fault` 注入在阶段边界抛出无法被任何阶段映射的异常，由
`_guard_stage` 捕获为 `E_UNATTRIBUTED`，进入最终报告的未归因清单，并
降级为零贡献占位树，保证异常不被静默吞掉。每条降级记录都带
`category` 与触发它的 `trigger_code`。

## 9. 业务码

| 业务码 | 含义 |
| --- | --- |
| `E_OK` | 正常 |
| `E_CONFIG_INVALID` | 配置非法（含字段、叶路径） |
| `E_CATEGORY_OUT_OF_RANGE` | 类别取值越界 |
| `E_DUPLICATE_THRESHOLD` | 候选阈值诱导重复划分 |
| `E_NONFINITE_HESSIAN` | 二阶导含非有限值 |
| `E_NEGATIVE_LEARNING_RATE` | 学习率为负 |
| `E_TOLERANCE_EXCEEDED` | 逐级误差超过声明容差 |
| `E_INCONSISTENT` | 双训复现/独立复算不一致 |
| `E_UNATTRIBUTED` | 未被任何阶段归因的异常 |

## 10. 独立重算契约

所有被记录结果都可从逐轮明细重算：

- 预测：`boosting.recompute_prediction` 以基础预测为起点，逐树取叶权重、
  乘学习率、按相同精度量化并遵守行隔离，结果与训练预测哈希一致。
- 损失：RMSE 由存储的逐轮预测与目标直接重算。
- 树结构：`TreeNode.to_dict` 为确定性嵌套字典，参与全局哈希。
- 归因：误差记录的 `(stage, round, row)` 键必须存在于偏差明细集合。

## 11. 运行方式

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v
python examples/run_demo.py
```
