# 远程教学考试周渐进发布离线复核内核

该项目用 Python 3 标准库实现一套可离线复核的渐进发布内核：

- 生成同一发布工程的 `v1`、`v2`、`v3` 三份历史 schema 样本；
- 把服务版本、按流量放量的步骤、观察窗口、缺失容忍和三类指标阈值归一；
- 按逻辑时刻计算滑动指标轨迹并逐步裁决放量、暂停或回滚；
- 回滚时选择最近已通过判定的稳定步骤，核算回收流量、重新观察窗口与受影响会话；
- 输出逐步裁决、指标轨迹、回退动作、放量账目和跨版本差异清单；
- 用另一份不共享判定代码的参考推进器逐步核对裁决、触发依据与回退目标。

## 快速运行

在工作区根目录执行：

```bash
python3 -m release_guard
```

等价入口：

```bash
python3 main.py
```

指定固定种子和目录：

```bash
python3 -m release_guard \
  --seed 20261127 \
  --samples-dir samples \
  --output-dir artifacts
```

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

成功时命令返回 `0` 并打印 `STATUS: PASS`；任一步骤归一、账目、回退目标或参考核对不一致时返回 `1`。

## 三版本 schema 与归一

生成器只使用固定种子 `20261127`，因此重复运行会得到完全相同的样本和报告。

| 归一字段 | v1 | v2 | v3 |
|---|---|---|---|
| 服务版本 | `service.release` | `release.artifact` | `artifact_version` |
| 步骤容器 | `rollout.steps[]`，`order` | `stages[]`，`sequence`，数组可乱序 | `plan.traffic_steps[]`，`phase` |
| 流量 | `traffic_percent`，百分数 | `traffic_fraction`，0..1 小数 | `traffic_ppm`，百万分率 |
| 观察窗口 | `observe_seconds` | `window.observe_minutes` | `observe_ms` |
| 滑动窗口 | `rolling_seconds` | `window.slide_minutes` | `rolling_ms` |
| 采样间隔 | `probe_interval_seconds` | `sampling.cadence_s` | `sample_ms` |
| 缺失容忍 | `max_missing_ratio` | `sampling.tolerance.missing_fraction` | `drop_limit_bp` |
| 连续超阈次数 | `required_consecutive_breaches` | `sustain` | `sustain_points` |
| 错误率阈值 | `thresholds.error_rate_percent` | `health.error_rate` | `threshold_expr.error`，如 `<= 1.00%` |
| 延迟阈值 | `thresholds.latency_p95_ms` | `health.latency_p95_ms` | `threshold_expr.latency`，如 `p95 <= 320 ms` |
| 掉线率阈值 | `thresholds.session_drop_percent` | `health.disconnect_rate` | `threshold_expr.session_drop`，如 `<= 0.80%` |
| 观测时间 | `at_seconds` | `at_ms` | `ts_ms` |
| 观测字段 | `requests` / `error_rate` / `latency_p95_ms` / `session_drop_rate` | `n` / `err` / `lat_p95_ms` / `drop` | `samples` / `error_pct` / `latency_p95` / `drop_pct` |

归一后的共同口径：

- 流量统一为整数 `traffic_ppm`，同时保留便于阅读的 `traffic_percent`；
- 时间统一为整数秒；
- 错误率和会话掉线率统一为 `0..1` 小数；
- 延迟统一为毫秒；
- 阈值统一为包含上界 `<=`，判定时使用严格大于阈值 `observed > threshold` 触发超阈；
- 每个归一字段都在 `normalized.json` 的 `provenance` 中记录来源 schema、来源路径、原字段名、原值、原单位、归一值和归一单位。

## 判定口径

每个步骤的观察窗口按声明时长依次排布。评估点位于步骤开始后的每个采样间隔；对时间 `t` 使用滑动区间 `(t - sliding_window, t]`，且不使用步骤开始前的样本。

- 错误率：按观测请求数加权的滑动均值；
- 延迟：对每次探针的 `p95_ms` 按请求数加权，计算 95% 最近秩分位数；
- 会话掉线率：按观测请求数加权的滑动均值。

每个轨迹点均记录：

- 滑动窗口起止边界；
- 期望样本点、实际样本点、缺失点数和缺失比例；
- 实际参与统计的请求数；
- 实际使用的样本时间列表；
- 当前滑动值、阈值、是否超阈和连续超阈计数。

缺失与恢复：

- 观察由若干滑动评估子窗口组成；当前窗口内某指标缺失比例严格大于步骤声明值时，立即暂停放量；
- 缺失指标的当前值写成 `null`，不沿用任何前一窗口值；
- 暂停期间不做超阈裁决，三类指标的连续超阈计数全部归零；
- 恢复后从恢复时刻重新从 `0` 累计连续超阈次数。

尖峰与持续劣化：

- 连续超阈次数小于声明次数的超阈片段，在回到阈值内、被缺失打断或窗口结束时计为一次 `spike_count`；
- 连续超阈次数达到声明次数时，只触发 `rollback`，该片段计入 `sustained_breach_count`，不计为尖峰；
- 账目中同时保留超阈评估点数量 `breach_count`、尖峰片段数 `spike_count` 和持续劣化片段数 `sustained_breach_count`。

回滚与账目：

- 回滚目标是回滚步骤之前最近一个最终裁决为 `promote` 的步骤；
- 回滚动作计算回收的 ppm/百分比、目标步骤的重新观察窗口、受影响会话数量和会话分组处置清单；
- 账目与裁决通过相同的 `step-N` 标识一一对应；
- `actual_rollout_seconds` 是已推进观察时间扣除暂停时间后的实际放量时长；
- 回滚后不再推进后续步骤，但这些步骤仍会在裁决和账目中标记为 `not_evaluated`，保证声明步骤全覆盖。

## 固定样本场景

默认种子生成的场景包含 5 个放量步骤：

| 步骤 | 流量 | 场景 | 预期结果 |
|---:|---:|---|---|
| 1 | 5% | 错误率单次尖峰后回落 | `promote`，尖峰 1 次，不因单点误回滚 |
| 2 | 15% | 延迟探针缺失超过 34% 后恢复 | 暂停 1 次、恢复后重新累计，最终 `promote` |
| 3 | 40% | 掉线率单次尖峰后回落 | `promote`，尖峰 1 次 |
| 4 | 70% | 错误率连续 3 个评估点超阈 | `rollback`，回退到步骤 3 的 40% |
| 5 | 100% | 前序已回滚 | `not_evaluated`，账目仍保留该步骤 |

当前样本的回滚核算结果：

- 回收流量：`30%` / `300000 ppm`；
- 重新观察窗口：逻辑时刻 `930` 到 `1170` 秒；
- 受影响会话：`3600` 个，按三类考试/监考会话分组处置。

## 输出文件

默认输出到 `artifacts/`：

```text
artifacts/
  report.md                          # 人工复核报告
  verification.json                  # 验收检查、哈希、首个参考不一致项
  cross_version_differences.json     # 完整跨版本差异清单
  summary.json                       # 命令结果、文件清单和关键摘要
  v1/
    normalized.json                  # 归一工程 + 逐字段 provenance
    run.json                         # 主流程完整结果
    reference.json                   # 独立参考推进器结果
    step_decisions.json / .csv       # 逐步裁决
    metric_trajectories.json         # 三类指标滑动轨迹
    rollback_actions.json            # 回退动作、会话处置和重新观察窗口
    ledger.json / .csv               # 放量账目
  v2/ ...
  v3/ ...
```

样本输入写入 `samples/schema_v1.json`、`samples/schema_v2.json`、`samples/schema_v3.json`。

## 独立参考推进器

`release_guard/reference_runner.py` 不导入主引擎、归一器或比较模块。它只读取相同的归一工程 JSON，独立重算：

- 滑动均值、加权 95% 最近秩分位数；
- 缺失比例、暂停与恢复状态；
- 连续超阈计数、尖峰与持续劣化；
- 每步裁决、触发依据和回滚目标。

`release_guard/comparison.py` 逐步骤比较主流程与参考结果。若不一致，`verification.json` 的 `first_reference_mismatch_by_schema` 会给出首个不一致步骤、字段、两侧取值和该步骤的指标依据。

## 源码结构

```text
main.py                          # 根目录命令行包装
release_guard/
  __main__.py                    # python -m release_guard
  cli.py                         # 参数解析和终端摘要
  generator.py                   # 固定种子三版本样本生成器
  normalizer.py                  # 三 schema 归一与 provenance
  engine.py                      # 主推进器、轨迹、回退和账目
  reference_runner.py            # 独立参考推进器
  comparison.py                  # 差异清单与首个不一致定位
  pipeline.py                    # 文件流水线和验收报告
  common.py                      # JSON、哈希和公共工具
tests/
  test_release_guard.py          # 端到端和关键口径测试
```

