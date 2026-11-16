# 实际验证结果

环境：Python 3.11.0rc1、NumPy 2.4.6（本地 wheel 安装）、Linux 容器开发，
代码仅用跨平台标准库，Windows 可直接用 `run_demo.bat` 或等价 `python` 命令运行。

## 单元测试

`PYTHONPATH=src python -m unittest discover -s tests`：**35 个用例全部通过**。

| 测试模块 | 覆盖点 |
| --- | --- |
| `test_fixedpoint` | floor 量化余数、乘除 1 ulp 截断、交叉相乘精确比较、链误差逐级增长、非法网格 |
| `test_ratelimiter` | 突发预填、整数/分数补充与余数结转、容量封顶零超发、逐位确定性轨迹 |
| `test_breaker` | 阈值触发、最小请求数、冷却拒绝、半开试探成功关闭/失败重开、整分钟桶滑出、超时与失败区分 |
| `test_degrade` | 选择器确定性、权重命中、多级回退、链条耗尽/零权重 E_DEGRADE、观测概率输入量化误差 |
| `test_validation` | 阈值越界、降级目标重复、时间戳回退、非法结果/负耗时、合法样本加载 |
| `test_snapshot_replay` | 恢复重放匹配、版本不一致整体替换、缺网格 E_GRID、网格变化 E_REPLAY、篡改首差异定位 |

## 端到端演示（55 个确定性事件）

- 决策分布：`pass=31, rate_limited=8, degraded=16`，无静默放行。
- `payment-api` 完整经历 closed→open→half_open→closed→open→half_open→open。
- 场景二版本号 1→2（失败率阈值 50→25），字段级 diff 正确辨识。
- 场景三快照 v1 与当前 v2 不一致时 `state_replaced=True`（整体替换，非增量合并），
  重放 55 个决策逐字段匹配 `replay matched=True`。
- 断点续写示例 `demo_continue.py`：在第 27 个事件切分，恢复后续写的 28 个决策
  与单引擎一次性运行的对应区间**逐位相同**。

## 误差界样例（grid exponent=4，S=10000）

- 令牌桶：`per_refill_truncation_ulp=1`、`cumulative_under_issue_ulp=1`、
  `over_issue_ulp=0`（floor 只欠不超，余数结转）。
- 降级链：单级端到端成功概率 `0.9950`，`total_error_bound_ulp=1`，
  `error_dominated=False`；多级时按 `e_m=e_{m-1}+输入量化+1` 合成。
- 熔断触发：定点失败率 `6666`（0.6666，1 ulp 量化误差），阈值 `50%` 整数交叉相乘比较，
  触发窗口标识 `window_minutes=[28333333]` 被记录。

## 业务码异常注入

| 注入参数 | 业务码 | 定位信息 |
| --- | --- | --- |
| `--inject bad-policy` | `E_POLICY` | 行号 + `services.payment-api.breaker.failure_threshold_percent` |
| `--inject bad-event` | `E_EVENT` | 行号 4 + `timestamp_ms` 回退明细 |
| `--inject bad-grid` | `E_GRID` | `grid.exponent` 字段缺失 |

三类错误均以 NDJSON 写入 `logs/structured.ndjson`。

## 打包验证

`python -m build --wheel` 成功产出 `dist/sre_guard-1.0.0-py3-none-any.whl`，
`--user` 安装后 `sre-guard` 控制台入口可直接运行三场景与异常注入。
