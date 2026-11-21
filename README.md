# motion-temporal

面向视频预测与运动时序建模的本地确定性领域组件。它把一次视频预测拆成四个可恢复阶段：

1. 帧对采样与时间轴展开
2. 运动编码与目标轨迹特征
3. 带历史窗口的递归时序聚合
4. 遮挡/掩码处理与逐帧输出

输出报告包含逐帧预测框、稀疏运动掩码、历史窗口摘要、归因记录、注入记录、质量指标、业务码、降级记录和检查点/快照信息。样本、真值、画布和掩码全部由固定种子在运行时生成，不下载数据集或模型权重。

## 环境

- Python 3.10+
- NumPy
- pytest（仅测试）

Windows 本地虚拟环境示例：

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install numpy pytest
```

Linux/macOS：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install numpy pytest
```

在仓库根目录直接运行即可，不需要安装其他运行时依赖：

```bash
python -m motion_temporal --help
```

也可以安装为可编辑包：

```bash
python -m pip install -e .
```

## 快速开始

### 1. 生成确定性本地样本

```bash
python -m motion_temporal generate --out data --frame-count 24 --seed 424242
```

生成：

- `data/manifest.jsonl`：序列清单，字段包括 `clip_id`、`frame_count`、`frame_interval_ms`、`object_count`、`motion_mode`、`seed`、`temporal_policy`、`model_revision`，并附 `truth_path`/`truth_checksum`。
- `data/truth/<clip_id>.jsonl`：逐帧真值框、遮挡标记、稀疏掩码坐标及掩码摘要校验和。
- `data/sample_summary.json`：清单与真值文件校验和摘要。

默认生成三段序列：匀速 `clip_uniform`、匀加速 `clip_accelerated`、周期运动 `clip_periodic`。

### 2. 正常运行

```bash
python -m motion_temporal run \
  --manifest data/manifest.jsonl \
  --output reports/normal.json \
  --cache-dir .cache
```

CLI 会打印摘要，完整报告写入 `reports/normal.json`。退出码：

- `0`：正常完成或帧边界优雅暂停
- `2`：业务错误

### 3. 验证报告

```bash
python -m motion_temporal verify --candidate reports/normal.json
```

验证器会从逐帧明细重算指标、校验预测内容校验和、检查跨阶段自检，并可选与基线报告逐字节比较：

```bash
python -m motion_temporal verify \
  --candidate reports/resumed.json \
  --baseline reports/normal.json
```

### 4. 注入异常

列出所有受控注入点：

```bash
python -m motion_temporal injections
```

时间戳倒挂，预期 `E_TIME_ORDER` 并定位到帧 7：

```bash
python -m motion_temporal run \
  --manifest data/manifest.jsonl \
  --output reports/time_order.json \
  --cache-dir .cache_time_order \
  --clip clip_uniform \
  --inject sampling:time_order:7
```

缺失观测 + 运动突变，预期正常运行且产生两类归因：

```bash
python -m motion_temporal run \
  --manifest data/manifest.jsonl \
  --output reports/attribution.json \
  --cache-dir .cache_attribution \
  --clip clip_uniform \
  --inject sampling:missing_observation:6 \
  --inject encoding:motion_jump:12
```

### 5. 优雅暂停与恢复

在聚合阶段第 8 帧边界暂停。采样和编码检查点已提交，聚合/输出未提交：

```bash
python -m motion_temporal run \
  --manifest data/manifest.jsonl \
  --output reports/paused.json \
  --cache-dir .cache_resume \
  --clip clip_uniform \
  --stop-at aggregation:8
```

使用同一个缓存目录再次运行相同命令（去掉 `--stop-at`）即可恢复：

```bash
python -m motion_temporal run \
  --manifest data/manifest.jsonl \
  --output reports/resumed.json \
  --cache-dir .cache_resume \
  --clip clip_uniform
```

### 6. 强制终止演示

`--delay-ms` 仅用于给外部终止留出时间窗口，不参与任何计算结果。Linux/macOS：

```bash
python -m motion_temporal run \
  --manifest data/manifest.jsonl \
  --output reports/killed.json \
  --cache-dir .cache_kill \
  --clip clip_uniform \
  --delay-ms 60 &
PID=$!
# 等本轮的 encoding 检查点出现后，在 aggregation 阶段执行：
kill -9 "$PID"
```

Windows 可用任务管理器或：

```powershell
Stop-Process -Id <PID> -Force
```

进程被强制终止时不承诺写出退出报告。随后使用同一缓存目录重跑即可从最后已提交阶段恢复。完整自动化演示：

```bash
python examples/demo.py
```

演示脚本会真实发送 `SIGKILL`，并比较恢复与不间断运行的预测校验和。

## 模块与接口

```python
from motion_temporal import (
    ClipSpec,
    PipelineConfig,
    SnapshotStore,
    aggregate_temporal,
    diagnose_attribution,
    encode_motion,
    evaluate_quality,
    expand_timeline,
    generate_clip,
    run_clip,
    sample_frame_pairs,
    validate_pipeline,
)
```

主要入口：

| 接口 | 作用 |
| --- | --- |
| `generate_clip(spec, missing_frames=())` | 固定种子生成画布、目标轨迹、真值框和稀疏掩码 |
| `expand_timeline(spec, frames)` | 校验时间戳顺序/间隔/跨度，展开相对时间轴 |
| `sample_frame_pairs(spec, timeline)` | 按 `temporal_policy` 生成帧对，稳定排序 |
| `encode_motion(spec, frames, sampling_payload)` | 掩码质心位移估计、速度/加速度/置信度特征 |
| `aggregate_temporal(spec, encoding_payload, ...)` | 历史窗口递归外推预测框与运动掩码 |
| `diagnose_attribution(...)` | 输出 `observation_missing` / `motion_abrupt` 归因 |
| `evaluate_quality(...)` | 位移误差、轨迹命中率、时间一致性 |
| `validate_pipeline(...)` | 跨阶段 clip/frame 集合与复用/重算集合自检 |
| `run_clip(...)` / `run_batch(...)` | 完整四阶段调度、检查点恢复、报告生成 |
| `SnapshotStore` | 模型参数、策略和历史状态快照的全量版本化存储 |

## 数据与确定性

所有内部对象先转为稳定 JSON：

- 字典键排序
- 紧凑分隔符
- 浮点统一保留 6 位小数
- 空值、NumPy 标量和数组显式转换

每个阶段和每个帧都带 SHA-256 校验信息。`run_id` 由算法修订号、序列清单、显式配置和注入计划共同计算，不依赖当前时间、进程状态或外部文件。

帧对排序键为：

```text
(dst_frame_index, interval_score, delta_ms, src_frame_index)
```

相同分数或相同间隔时，`src_frame_index` 升序打破平局。所有策略都保留相邻主帧对；`stride2`、`pyramid`、`adaptive` 额外加入多步长帧对。

## 时间轴错误

- `E_TIME_ORDER`：时间戳倒挂或 `frame_index` 重复；报错携带 `clip_id` 和帧位置。
- `E_TIME_SPAN`：帧数量、相邻时间间隔或总时间跨度与配置不符；报错携带 `clip_id` 和首个不一致帧位置。

## 运动编码

- 由帧对图像亮度加权的稀疏掩码质心做确定性位移估计。
- 每目标输出位移、速度、加速度、掩码支持度、外推置信度和最后有效帧。
- 遮挡目标保留占位并标记为 `occluded`，`velocity/speed/displacement` 为 `null`，不参与速度更新。
- 源帧不可用时，使用最近一次占位框做显式跨帧回退，仍保持可追溯帧位置。
- 目标数超过 `object_count`、重复/越界目标 ID 返回 `E_OBJECT_OVERFLOW`。
- 掩码维度或坐标越界返回 `E_MASK_SHAPE`。

## 递归聚合与可追溯性

每个输出帧包含：

- 预测框、遮挡状态、来源（观测/缺失外推/遮挡外推）、置信度、外推年龄
- 历史窗口起止帧、窗口长度、窗口摘要哈希、历史状态哈希列表
- 当前状态哈希
- 源帧对、采样帧对

历史窗口参与缺失帧的速度估计，衰减系数逐帧作用于速度和加速度。缺失或遮挡时所有递归中间状态只存在内存；未提交阶段被终止后必从该阶段第 0 帧重建。

## 归因

受控类别只有：

- `observation_missing`：整帧缺失或目标遮挡/缺失，输出外推年龄与受影响目标。
- `motion_abrupt`：目标加速度超过阈值，输出偏差量、阈值和受影响目标。

每条归因包含发生帧、历史窗口摘要、状态哈希和偏差明细。类别不受控或帧位置无法定位时返回 `E_ATTRIBUTION_FAIL`，并在 `located_frames` 中保留已定位帧。

## 检查点与恢复

四个阶段分别提交检查点：

```text
sampling -> encoding -> aggregation -> output
```

规则：

- 只有阶段所有帧完成才提交，`complete=true`。
- 检查点版本号全局单调递增（正常从头运行时为 1、2、3、4）。
- 检查点包含 payload 校验和与 envelope 校验和。
- 重启时只加载连续且校验通过的前缀。
- 已提交阶段直接复用，不重算。
- 当前未提交阶段及其递归状态全部丢弃，从该阶段起点重建。
- 恢复后输出帧集合与不间断运行的预测内容逐字节一致。

## 快照防护

快照类别：

- `model_parameters`
- `temporal_policy`
- `history_state`

每个快照包含类别、版本号、内容校验和和 envelope 校验和。写入始终是完整快照原子替换，不做增量修补。较低版本覆盖较高版本返回 `E_SNAPSHOT_ROLLBACK`；文件损坏或校验和不符返回 `E_SNAPSHOT_CORRUPT`，并指明类别和版本号。

## 四阶段注入点

| 阶段 | 注入 kind | 预期业务码 |
| --- | --- | --- |
| sampling | `time_order` | `E_TIME_ORDER` |
| sampling | `time_span` | `E_TIME_SPAN` |
| sampling | `missing_observation` | `E_OBSERVATION_MISSING` |
| encoding | `mask_shape` | `E_MASK_SHAPE` |
| encoding | `object_overflow` | `E_OBJECT_OVERFLOW` |
| encoding | `motion_jump` | `E_MOTION_ABRUPT` |
| aggregation | `window_zero` | `E_WINDOW_INVALID` |
| aggregation | `invalid_decay` | `E_DECAY_INVALID` |
| aggregation | `extrapolation_oob` | `E_EXTRAPOLATION_OOB` |
| output | `drop_frame` | `E_INCONSISTENT` |
| output | `mask_shape` | `E_MASK_SHAPE` |

注入只通过 CLI 参数或 `InjectionSpec` 施加到输入/中间数据，不修改源码。每条注入记录含阶段、kind、业务码、`clip_id`、帧位置、参数和应用状态。

## 指标

- 位移误差：`mean(Euclidean(predicted_delta - truth_delta))`
- 轨迹命中率：`mean(IoU >= threshold)`
- 时间一致性：`1 / (1 + mean(abs(predicted_delta2 - truth_delta2)))`

报告包含逐帧/逐目标明细，`verify` 会从 `metrics.details` 独立重算并逐项比较。

## 资源限制

`PipelineConfig` 提供：

- `max_sequence_frames`
- `max_history_windows`
- `max_parallel_clips`

`resource_policy=error` 时超限返回 `E_RESOURCE_LIMIT`；`resource_policy=degrade` 时按明确策略截断并写入 `sequence_truncated` 或 `history_window_truncated` 降级记录。

## 跨模块自检

输出提交前执行：

- 各阶段 `clip_id` 完全一致
- 各阶段帧索引集合完全一致且等于期望集合
- 复用集合与重算集合无交集
- 两者并集覆盖受影响帧
- 复用/重算索引不越界

任一失败返回 `E_INCONSISTENT` 并列出类别、缺失帧和越界索引。

## 目录

```text
motion_temporal/
  aggregation.py   # 历史窗口递归聚合
  attribution.py   # 缺失/突变归因
  checkpoint.py    # 四阶段检查点
  cli.py           # 命令行
  encoding.py      # 运动编码
  generator.py     # 确定性样本与 JSONL 读写
  injections.py    # 四阶段注入框架
  masks.py         # 稀疏掩码与摘要
  metrics.py       # 质量指标
  output.py        # 最终逐帧输出
  pipeline.py      # 调度、恢复、报告
  sampling.py      # 时间轴与帧对采样
  selfcheck.py     # 跨阶段一致性
  snapshots.py     # 版本化快照存储
examples/
  demo.py          # 正常/注入/SIGKILL 恢复演示
tests/
docs/
  VERIFICATION.md  # 实际验证记录
```

## 测试

```bash
python -m pytest -q
```

测试覆盖采样确定性、时间错误、编码/遮挡/溢出、聚合校验、归因、指标重算、快照回退与损坏、检查点恢复、输出自检、资源限制、CLI 端到端和真实 `SIGKILL` 恢复。

实际验证结果见 `docs/VERIFICATION.md`。
