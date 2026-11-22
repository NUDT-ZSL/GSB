# 本地时序精度与误差传播管线

这是一个不依赖网络、真实数据或外部服务的确定性时序预测验证项目。它从固定种子生成多变量时间序列，完成窗口切分、特征工程、分块训练、递归预测、误差传播、门限判决、版本化快照和跨级一致性自检，最终输出可审计的 JSON 报告。

核心约束：

- Python 3.10+，仅额外依赖 NumPy 1.24+。
- 所有随机源使用显式固定种子。
- 训练核心中间量统一为 float64；精度档只改变输入、目标和模型参数边界的量化。
- 不访问未来真实目标值，真实未来值只用于预测完成后的评估。
- 同配置重复运行时，窗口数组、特征数组、训练轮次、预测和报告字节一致。

## 项目结构

```text
.
├── configs/sample_config.json       # 样例预测配置
├── data/sample_series.json          # 已生成的确定性本地样本
├── scripts/generate_sample.py       # 样本生成脚本
├── scripts/verify_report.py         # 已生成报告的离线一致性复核
├── ts_pipeline/
│   ├── config.py                    # 配置合并与严格校验
│   ├── errors.py                    # 稳定业务错误码
│   ├── series.py                    # 确定性序列生成/持久化
│   ├── windows.py                   # 窗口切分与特征算子
│   ├── scaling.py                   # 仅训练集拟合的标准化
│   ├── precision.py                 # float64/float32/定点量化
│   ├── models.py                    # 线性 AR 与树桩提升模型
│   ├── training.py                  # 分块/整段训练与逐轮对照
│   ├── forecast.py                  # 递归预测与诊断开环预测
│   ├── evaluation.py                # 门限与指标聚合
│   ├── spectrum.py                  # 逐级误差谱与归因
│   ├── snapshots.py                 # 原子快照、版本、校验和
│   ├── consistency.py               # 跨级一致性自检
│   ├── pipeline.py                  # 端到端编排
│   └── cli.py                       # 命令行入口
├── tests/test_pipeline.py            # 15 个自动化验证
├── main.py                           # python main.py 入口
├── reports/sample_report.json        # 已生成验证报告
└── VALIDATION.md                     # 实际验证记录
```

## 安装与快速运行

Windows PowerShell/CMD：

```bat
py -3 -m pip install -r requirements.txt
py -3 scripts\generate_sample.py --config configs\sample_config.json --output data\sample_series.json
py -3 main.py --config configs\sample_config.json --series data\sample_series.json --output reports\report.json --print-summary
```

Linux/macOS：

```bash
python3 -m pip install -r requirements.txt
python3 scripts/generate_sample.py --config configs/sample_config.json --output data/sample_series.json
python3 main.py --config configs/sample_config.json --series data/sample_series.json --output reports/report.json --print-summary
```

也可以使用模块入口：

```bash
python -m ts_pipeline --config configs/sample_config.json --output reports/report.json
```

离线复核已有报告：

```bash
python scripts/verify_report.py --report reports/sample_report.json
```

切换精度档或门限策略：

```bash
python main.py --config configs/sample_config.json --profile float32 --output reports/float32.json
python main.py --config configs/sample_config.json --profile fixed_point --output reports/fixed.json
python main.py --config configs/sample_config.json --strategy strict --output reports/strict.json
```

常用参数：

- `--profile {float64,float32,fixed_point}`：覆盖 `precision.profile`。
- `--chunk-size N`：覆盖训练分块大小。
- `--strategy {strict,tolerance_band}`：覆盖门限策略并写入策略历史。
- `--threshold`、`--tolerance`：覆盖门限和容差。
- `--snapshot-dir`：覆盖快照目录。
- `--snapshot-version N`：为全部快照指定显式版本，用于回滚测试。
- `--no-snapshots`：只运行计算，不读写快照。
- `--requested-steps N`：诊断递归深度，超过 horizon 会返回 `E_RECURSION_INVALID`。

## 确定性约定

### 样本生成

- 目标噪声流种子为根配置 `seed`。
- 每个外生变量和事件列使用配置中的独立显式 `seed`。
- 目标、外生变量和事件数组均为 float64；事件列仅含 0/1。
- 目标含趋势、周期、噪声，以及可选的上一期事件冲击。
- 内容摘要基于数组形状和 C 顺序 IEEE-754 float64 字节，不依赖 JSON 浮点排版。

### 窗口与特征索引

设序列长度 `N`、窗口长度 `L`、预测步长 `H`、窗口起始索引 `s`：

```text
history   = [s, s + L)
target    = [s + L, s + L + H)
lookback  = max(lag - 1, rolling_window - 1, diff_order)
features  = anchors [s + lookback, s + L)
```

第 `t` 个特征锚点使用截至 `y[t]` 的滞后、滑动统计和差分，以及索引 `t` 的外生/事件列；其后一个目标为 `y[t+1]`。

训练窗口满足：

```text
s + L + H <= split_index
```

测试窗口满足：

```text
s >= split_index
```

因此所有训练窗口的完整范围都在所有测试窗口之前，不存在时间重叠。窗口按起始索引升序编号为 `w000000`、`w000001` 等。

特征列顺序固定为：

```text
lag_* -> rolling_* -> diff_* -> exog_* -> event_*
```

精度档切换不会改变窗口集合、边界、特征列顺序或标签顺序。

## 分块训练

训练行按 `(窗口起始索引, 特征锚点索引)` 排列。每轮执行：

1. 按 `training.chunk_size` 顺序遍历块。
2. 每块按行顺序计算损失和原始梯度。
3. 按块顺序归约梯度；`compensated_sum=true` 时使用 Neumaier 补偿求和。
4. 整轮梯度统一除以总样本数，L2 罚项整轮只加一次。
5. 每轮更新一次参数，再应用模型参数精度量化。

报告同时包含：

- 每轮损失、完整梯度向量、梯度范数、块数、各块损失和梯度摘要。
- 同输入的 float64 一次性整段训练轮次。
- 每轮损失绝对/相对偏差、参数最大绝对偏差、参数 L2 偏差。
- 最终模型和完整轮次的 SHA-256 摘要。

`training.reduction_order` 当前受控取值为 `window_then_anchor`，报告会显式记录损失与梯度的归约顺序。

## 精度档

三档共享同一套算子与归约顺序，差异只来自边界量化：

| 档位 | 特征/目标边界 | 模型参数边界 |
|---|---|---|
| `float64` | 原值 | 原值 |
| `float32` | cast 到 float32 再回 float64 | 同左 |
| `fixed_point` | `round(x*scale)/scale` | 同左 |

定点舍入支持：

- `nearest_even`
- `nearest_away`
- `toward_zero`
- `floor`
- `ceil`

报告中的 `precision.quantization_audit` 记录原始值、缩放因子、整数编码、反量化值和独立重放值。定点偏差可用以下公式独立复算：

```text
q = round(original * fixed_scale, rounding)
dequantized = q / fixed_scale
```

## 递归预测

每个测试窗口从 `s + L - 1` 锚点开始，依次预测 `H` 步：

- 第 1 步使用真实历史 `[s, s+L)`。
- 后续步骤只把此前预测值追加到历史，用于滞后、滑动统计和差分。
- 递归执行期间记录的最大真实目标索引始终小于 `s+L`。
- 真实目标值只在预测完成后附加到报告，字段标记为 `evaluation_only_after_prediction`。
- 外生变量/事件列按当前锚点读取，但未来真实目标值不会被读取。

每步输出：

- 预测值、置信区间上下界。
- 相对/绝对误差、累计递归绝对误差。
- 门限判决、保护标记、跳过标记。
- 递归反馈差值和仅用于诊断的开环预测值。

请求深度非法或检测到未来目标访问时返回 `E_RECURSION_INVALID`，并包含 `window_index`、`window_id` 和 `step`。

## 误差谱与归因

参考真值是同一输入上的 float64、无量化、整段训练模型递归预测。误差谱固定包含五个受控级别：

| 级别 | 隔离内容 |
|---|---|
| `window_boundary` | 窗口 ID、边界和行顺序 |
| `feature_scaling` | 缩放特征边界的精度量化 |
| `chunk_gradient_accumulation` | 分块模型训练相对参考模型的差异 |
| `fixed_point_quantization` | 定点模型参数相对量化前模型的差异；非定点档为零 |
| `recursive_prediction` | 递归反馈相对同模型开环预测的差异 |

每个级别输出：

- 绝对偏差和相对偏差。
- 由逐窗口 RMS 偏差重算的误差区间。
- 相对上一级的误差放大比。
- 每个测试窗口的偏差明细。
- 总误差相对 float64 参考的独立视图。

主导来源取绝对误差最大的受控级别；若所有级别均为零，则取 `none`。非法级别或区间上界小于下界返回 `E_ERROR_SPECTRUM_INVALID`，并指出窗口和级别。

## 门限与指标

`strict`：

```text
abs_error < threshold
```

`tolerance_band`：

```text
abs_error <= threshold + tolerance / 2
protected = abs(abs_error - threshold) <= tolerance
```

容差带策略把判决中点固定在门限带中央，因此恰好落在门限附近的跨精度差异不会因末位舍入翻转。`training.subnormal_guard=true` 时，float64 次正规数会刷新为零。

指标包括 `mae`、`rmse`、`mape_percent`、`smape_percent`、`max_abs_error`、`threshold_pass_rate` 和区间覆盖率。所有指标都由逐窗口逐步明细按显式顺序重算，并纳入一致性检查。

门限或容差为负、非有限值或策略未知时返回 `E_CONFIG_INVALID`，并指出具体字段。

## 版本化快照

每次运行会维护三个独立快照：

- `window_stats`
- `precision_config`
- `error_spectrum`

快照文件格式：

```json
{
  "format": "ts-precision-snapshot/v1",
  "snapshot_name": "window_stats",
  "version": 1,
  "checksum": "...",
  "payload": {}
}
```

行为：

- `checksum` 是规范化 payload 的 SHA-256。
- 每个快照整体写入临时文件，再通过同目录原子替换，不做增量修补。
- 相同内容重复运行保持原版本。
- 内容变化时版本单调递增。
- 重启后通过 `SnapshotStore.sync` 检查版本和校验和，变化时整体替换。
- 旧版本或同版本不同内容覆盖返回 `E_SNAPSHOT_ROLLBACK`。
- 文件损坏、格式非法或校验和不符返回 `E_SNAPSHOT_CORRUPT`，并指出快照名和版本。
- 门限策略变化会追加到 `threshold_strategy_history`，并随精度配置快照持久化。

## 一致性自检

报告写出前执行：

1. 窗口阶段、训练阶段、预测阶段和误差谱的窗口 ID 集合/顺序一致。
2. 每步预测可由执行特征和模型参数独立复算。
3. 误差谱可由逐窗口偏差明细重算。
4. 指标可由预测明细重算。
5. 保护/跳过判决与门限策略执行一致。

不一致返回 `E_INCONSISTENT`，`position` 指向首个偏差字段，例如：

```text
predictions.windows[3](w000123).steps[2].prediction
```

## 业务错误码

| 错误码 | 含义 |
|---|---|
| `E_CONFIG_INVALID` | 配置字段非法 |
| `E_RECURSION_INVALID` | 递归深度非法或访问未来目标 |
| `E_ERROR_SPECTRUM_INVALID` | 误差区间或级别非法 |
| `E_SNAPSHOT_ROLLBACK` | 尝试用旧/相同版本覆盖新内容 |
| `E_SNAPSHOT_CORRUPT` | 快照损坏或校验和不符 |
| `E_INCONSISTENT` | 跨级复算不一致 |

CLI 业务错误会写入输出 JSON，并以退出码 2 结束。

## JSON 报告概览

`reports/sample_report.json` 包含：

- `sample`：生成参数、统计量、数组摘要和内容校验和。
- `windows`：窗口划分统计、完整窗口清单、特征列和逐窗特征摘要。
- `feature_scaling`：训练集均值/标准差及摘要。
- `training`：逐轮损失/梯度、分块明细、整段对照、模型参数和摘要。
- `precision`：当前精度档、定点审计和独立复算公式。
- `analysis_reference`：float64 整段训练参考快照。
- `error_spectrum`：五级误差谱、区间、放大比和主导来源。
- `predictions`：逐测试窗口逐步预测、区间、误差和判决。
- `metrics`：聚合指标。
- `threshold_policy`：当前门限策略及变更历史。
- `consistency`：五项跨级自检结果。
- `reproducibility`：窗口、训练、预测和误差谱摘要。
- `snapshots`：快照版本和校验和。
- `deterministic_core_sha256`：排除快照元数据后的报告核心摘要。

## Python API

```python
from ts_pipeline import load_config, generate_series, run_pipeline

config = load_config("configs/sample_config.json")
series = generate_series(config)
report = run_pipeline(config, series=series, write_snapshots=False)

assert report["business_code"] == "OK"
assert report["consistency"]["status"] == "OK"
print(report["metrics"]["mae"])
print(report["error_spectrum"]["dominant_source"])
```

快照回滚测试：

```python
from ts_pipeline import SnapshotRollback, SnapshotStore

store = SnapshotStore("reports/test_snapshots")
store.commit("window_stats", {"count": 1})
store.commit("window_stats", {"count": 2})

try:
    store.commit("window_stats", {"count": 3}, version=1)
except SnapshotRollback as exc:
    assert exc.code == "E_SNAPSHOT_ROLLBACK"
```

## 测试

```bash
python -m unittest discover -v
```

测试覆盖：窗口/特征逐字节确定性、训练与整段对照、两种模型族、三档精度共享布局、定点独立复算、递归未来隔离、误差谱异常、门限稳定性、指标/预测/误差谱篡改检测、快照整体替换/回滚/损坏、重启版本同步和 CLI 业务码。

实际验证环境与结果见 [VALIDATION.md](VALIDATION.md)。
