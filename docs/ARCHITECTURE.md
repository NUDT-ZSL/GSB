# 架构与诊断语义

## 1. 确定性样本

`diagnostic_engine/generator.py` 使用 `numpy.random.default_rng(440)` 生成 10 个变量：

- 采样间隔按变量从 200ms 到 600ms 变化；
- 每条记录的 `values` 含 4 个 25ms 子采样点；
- 噪声包含高斯和部分 Laplace 重尾分量；
- 固定时间段制造缺失段；
- 每 13 条左右注入离群点；
- 8000ms 后加入分段局部趋势。

`v_idx` 在所有原始记录按 `(obs_time_ms, src_name, record_number)` 稳定排序后顺序分配，因此固定种子下 JSONL 字节稳定。

## 2. 窗口与补全

默认窗口为 `[start_ms, end_ms)`，长度 3200ms，共 32 个 100ms 网格。记录中的子采样点按真实时间落入网格；同一网格多个值取确定性均值。

- `ffill`：缺失网格沿用上一观测值；窗口起始无前值时使用配置常量。
- `constant`：缺失网格使用配置常量。
- 每个缺失点输出 `missing_mask`、距上次观测的距离和 `noise_decay ** distance` 衰减标记。

每变量输出均值、总体方差、标准差、0.05/0.25/0.5/0.75/0.95 分位数、IQR、缺失数和平均 `q_param`。

## 3. 交叉与条件标准化

slot 声明变量集合、arity 和 op。引擎用稳定的 `itertools.combinations` 枚举两两或三三组合，列名遵循：

```text
{slot}#{combo}#{op}
```

支持：

- 两两 `product`、`difference`；
- 三三 `triple_product`；
- `raw` 与 `signed_log1p` 编码。

条件标准化以每个交叉列的首个组合变量为条件变量，用稳定排名把行分配到配置数量的条件箱。`zscore` 使用箱内均值/标准差，`robust` 使用中位数/IQR。零尺度直接返回 `E_STD_ZERO`，不引入会掩盖配置问题的 epsilon。

## 4. 降维与协方差

- `pca`：对中心化矩阵做确定性 SVD，并按最大绝对分量符号校正投影方向。
- `random_projection`：由候选 seed 生成稀疏 Rademacher 投影，密度和 seed 均来自配置。
- `pca_then_random_projection`：先 PCA 到 `intermediate_dim`，再通过稀疏随机矩阵投影到输出维。

行权重为 `decay ** (rows - 1 - row)`，最近一行权重为 1。协方差对角是归一化指数衰减加权二阶中心矩。

## 5. 预测与误差谱

预测器是显式分位线性模型：

```text
Q_q(t) = Z(t) @ coefficient[q] + intercept[q]
```

目标是配置目标变量在下一网格行的标准化值，因此使用前 `T-1` 个降维坐标预测后 `T-1` 个目标。残差定义为实际值减中位数分位预测。

误差谱包含：

- 每个分位的 MAE、RMSE、均值误差、pinball loss 和覆盖率；
- 严格分位边界比较与容差带比较；
- 严格/容差门控、有效边界和门限附近样本；
- 残差均值/方差/EWMA、特征均值漂移、联合 drift score 和门开关。

## 6. 精度语义

`Precision.q(x)` 是所有算子共用的表示边界：

- float64：转换为 `float64`；
- float32：先转 `float32` 再提升回 `float64` 参与统一 NumPy 算子；
- fixed：执行 `round(x * scale)` 得到 `int64` raw value，再除以 scale 形成量化网格上的物理值。

矩阵乘、加减、乘法、除法、均值、方差和衰减权重都经过相同量化出口。算法分支只读取配置，不读取精度档名称，因此精度档不会改变执行路径。

## 7. 恢复一致性

快照目录：

```text
.diagnostic/state/snapshots/<window>__<profile>/snapshot-0000000N.json
.diagnostic/state/snapshots/<window>__<profile>/latest.json
```

恢复流程：

1. 删除所有 `*.tmp-<pid>` 临时文件；
2. 读取所有已提交快照中的 `cache_keys`；
3. 删除不被快照引用的 cache JSON 和 SQLite artifact 行；
4. 每个窗口从 `latest.json` 检查点之后继续；
5. 已提交节点的内容键仍存在，因此动作是 `reused`，不会重新计算或重复计入 `completed_v_idx`。

测试中的 `--sigkill-after-commit N` 在第 N 次快照原子提交后向自身发送 `SIGKILL`。这与外部 `kill -9` 一样无法执行清理钩子；重启等价于真实强制终止恢复。
