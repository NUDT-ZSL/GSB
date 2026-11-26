# Telemetry Drift

面向多源遥测变量的在线预测器漂移诊断首版。项目只使用本地固定种子样本，
不访问外部服务、真实账号或数据集。核心产物包括：

- 确定性双边变量对齐与滑动残差窗口；
- 衰减均值/方差/偏度/趋势统计与协变量反传异常贡献；
- 漂移事件的最小相邻闭包和闭包局部增量岭回归；
- v1 单文件 JSON、v2 分片 JSON + SQLite 元数据快照互迁；
- float64 / float32 / 定点整数三档一致的联合门槛裁决；
- 内容寻址缓存、依赖闭包失效、原子阶段检查点、提交互斥锁；
- 可独立重放的校验账本和端到端验证 CLI。

## 快速开始

要求 Python 3.10+、NumPy 1.24+。

```bash
python3 -m pip install -r requirements.txt

# 生成固定种子输入到 data/
python3 scripts/generate_data.py --out data

# 运行 v1 预测器并输出 result/aligned/windows/ledger/summary
python3 -m telemetry_drift run \
  --config data/predictor_config.json \
  --observations data/observations.jsonl \
  --snapshot data/predictor_v1.json \
  --out artifacts/run-v1 \
  --cache-dir .cache/run

# 完整验收：v1/v2 一致性、账本重放、迁移、缓存、检查点、错误码
python3 -m telemetry_drift verify \
  --data data \
  --out artifacts/verification

# 测试
python3 -m pytest
```

也可 `pip install -e .` 后直接使用 `telemetry-drift` 命令。

## 目录

```text
telemetry_drift/
  alignment.py       双边 as-of 对齐、公共网格、缺失插补
  statistics.py      衰减滑窗统计与漂移分数
  thresholds.py      严格/容差带、三档精度、次正规数保护
  windows.py         输出源原生采样点上的残差回溯窗口
  retraining.py      漂移事件、最小闭包、闭包局部增量重训练
  snapshots.py       v1/v2 读取分派、互迁、SQLite 元数据
  cache.py           内容寻址缓存与依赖闭包传播
  checkpoint.py      阶段状态机、原子检查点、显式保存点
  ledger.py          校验账本与独立重放
  pipeline.py        端到端管线与 v1/v2 语义比较
  samplegen.py       固定种子样本生成
  cli.py             运行、迁移、账本重放、验收入口
scripts/generate_data.py
tests/
```

## 输入格式

`data/observations.jsonl` 每行是一条观测：

```json
{"logical_ms":0,"source":"vehicle-can","values":[0.0136],"variable":0}
```

变量 0/1 是 10 Hz 输入源，变量 2 是 5 Hz 输出源，变量 3 是未参与预测的
辅助源。样本包含空值、离群点和 1800-2600 ms 的局部变化段。

`data/predictor_config.json` 主要字段：

- `alignment`：公共网格周期、as-of 最大陈旧时间、缺失策略；
- `residual_window`：窗口大小、步长、最大回溯；
- `statistics`：衰减系数、偏度/趋势/上游权重、残差与输入尺度；
- `thresholds`：`strict` 或 `tolerance`、容差、定点缩放、次正规下限、
  `both/any` 联合规则；
- `retrain`：闭包半径、岭回归正则、重训练代数；
- `predictor`：输入/输出变量、漂移门槛、输入幅度门槛、回收率阈值。

## 算法约定

### 对齐

公共索引由配置周期和逻辑毫秒构造。变量按 as-of 规则取最近历史观测；
超过 `stale_after_ms` 或原始值为空时进入缺失处理。支持：

- `forward_fill`：持有最近有效值，前缘使用最近后值；
- `minimal_change`：两个有效点之间线性插值，边缘使用最近有效值。

输出 JSON 使用排序键、紧凑分隔符和稳定浮点量化，因此同一输入与配置可得到
逐字节一致的对齐文件。预测残差只在输出源的原生采样周期上计算，避免把低采样
率输出的采样保持误判成漂移。

### 窗口分数

对窗口残差 `r_i`，输入特征变化 `Δx_ij` 和预测器系数 `c_j`：

```text
u_i = sum_j(|c_j| * |Δx_ij|) / input_scale
z_i = r_i / residual_scale + upstream_weight * u_i
w_i = decay^(n-1-i)
```

分数仅由窗口原始残差、窗口特征变化、系数和统计参数决定：

```text
score = |weighted_mean(z)|
      + sqrt(weighted_var(z))
      + skew_weight*|weighted_skew(z)|
      + trend_weight*|weighted_trend(z)|
      + upstream_weight*weighted_mean(|u|)
```

没有全局预测器状态或隐式窗口截断参与。`recompute_drift_score` 和账本重放均
从这些显式材料独立复算。

### 门槛

严格策略只接受明确大于门槛；容差带策略要求大于 `threshold + tolerance`。
每个数值同时构造 float64、float32 和定点整数表示。三档原始判决不一致时，
使用原始 float64 参考值的十进制展开作精确回退，并记录
`reference_fallback=true`，从而避免门限附近的舍入翻转。绝对值不超过
`subnormal_floor` 的数值归零。

### 闭包与重训练

超过联合门槛的窗口成为漂移事件。事件按 `(-score, start_ms, window_id)` 排序，
向左右扩展 `closure_radius` 并合并重叠/相邻窗口。闭包样本集合只取覆盖窗口
样本的并集；从旧模型出发，用闭包样本和 L2 正则求解增量岭回归。闭包外窗口
预测直接复用旧结果，不做任何改写。

## 快照与迁移

v1 是单文件 JSON：

- `version: "v1"`；
- 相对秒时间；
- `parameters` 中的扁平系数/截距；
- 单个预测器。

v2 是 manifest JSON + 模型分片 JSON + SQLite：

- manifest 有版本头、分片路径/校验和、当前模型；
- 模型记录绝对毫秒、配置代数和重训练代数；
- SQLite `metadata` 保存代际元数据；
- `completion_log` 记录升级补全字段及来源；
- `tombstones` 记录失效代数。

读取时按版本头分派。未知版本抛出 `E_VERSION_UNSUPPORTED`。v1 升级会生成
v2 并写入补全来源；v2 降级时，v1 无法表达的增量重训练代数被显式丢弃，输出
`E_INCREMENTAL_RETRAIN_DROPPED` 记录并嵌入 v1 文件的
`dropped_incremental_generations`。

## 缓存、并发与检查点

窗口缓存键包含：

- 输入/输出变量内容哈希；
- 预测器快照版本；
- 残差窗口索引范围；
- 回收率阈值；
- 重训练配置摘要。

此外有整次运行的内容键。`changes` 可声明变量修订区间、预测器变化或窗口尺寸
变化；系统计算预测器闭包、受影响窗口集合并只重算这些窗口，输出
`recomputed_windows` / `reused_windows` 清单。

所有阶段和快照提交使用同一个非阻塞锁域。默认锁路径是
`TELEMETRY_DRIFT_COMMIT_LOCK`，未设置时为当前工作目录的
`.telemetry-drift.commit.lock`；也可显式传入 `lock_path`。冲突立即返回
`E_STAGE_BUSY`，不会等待。

阶段检查点只在完整阶段结束时写入。写入流程为：同目录临时文件、`fsync`、
`os.replace`、目录 `fsync`。快照包含单调版本号、已完成阶段/记录集合和内容
checksum。强制终止只会留下不可见的临时文件，恢复时回到最近已提交阶段。
信号进入 `exit_report` 后可显式恢复命名保存点。

## 账本

每个闭包账本条目记录：

- 覆盖窗口和事件窗口；
- 重算范围（索引与毫秒）；
- 各窗口漂移分数；
- 原始残差、特征变化与系数；
- 重训练样本、目标、模型前后态；
- 新旧预测、业务码和结果摘要。

`replay-ledger` 不使用原运行内存状态，逐条重算统计、重训练和替换预测，再核对
checksum/摘要。任何偏差返回 `E_LEDGER_REPLAY`。

## 错误码

| 错误码 | 含义 |
|---|---|
| `E_VERSION_UNSUPPORTED` | 未知快照版本 |
| `E_STAGE_BUSY` | 阶段/快照提交锁被占用 |
| `E_CHECKPOINT_CORRUPT` | 检查点结构、版本或 checksum 无效 |
| `E_CONFIG_INVALID` | 配置字段或取值无效 |
| `E_ALIGNMENT_INVALID` | 观测流、变量或对齐结果无效 |
| `E_SNAPSHOT_INVALID` | 快照结构、分片或元数据无效 |
| `E_CACHE_CORRUPT` | 缓存键、payload 或 checksum 不一致 |
| `E_LEDGER_REPLAY` | 账本无法独立重放出相同结果 |

## 实测结果

本机实际验证记录见 `VALIDATION.md` 和
`artifacts/verification/verification.json`。固定样本得到 5 个窗口、3 个漂移
事件、1 个合并闭包；闭包覆盖 `w0001-w0004`，`w0000` 预测严格复用。
v1/v2 的语义摘要一致，26 项 pytest 全部通过，验收命令 12 项检查全部通过。

