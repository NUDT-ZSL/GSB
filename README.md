# SRE 错误预算燃烧率告警引擎

面向本地 Windows/Python 3.11+ 的确定性领域组件。输入是按分钟聚合的 SLI
事件流与 SLO 配置，输出滚动窗口 SLI、错误预算、多窗口燃烧率、三级告警历史，
并支持会话快照、整体替换恢复和历史评估逐位重放。

核心设计取舍：**内部全部使用精确有理数，公开比例/燃烧率只在输出边界量化一次**。
因此重放比较的是整数定点值、误差界和告警状态，不依赖平台浮点运算。

## 快速开始（Windows）

```bat
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -e .
.venv\Scripts\python examples\demo.py
.venv\Scripts\python -m unittest discover -s tests -t . -v
```

若 Windows 没有 `py` launcher，把 `py -3.11` 改为已安装的 `python` 即可；其余命令
不变。项目不依赖 Docker、服务端或外部账号。

Linux/macOS 对应命令：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .
.venv/bin/python examples/demo.py
.venv/bin/python -m unittest discover -s tests -t . -v
```

演示会顺序执行四个场景并生成 `examples/out/demo_report.json` 与
`examples/out/cold_session.snapshot.json`：

1. 冷评估；
2. 参数变更后评估；
3. 快照恢复（模拟当前数据版本变化，执行整体替换）；
4. 从快照恢复后的历史重放校验。

## 输入数据

推荐大文件使用 NDJSON，每行一个对象：

```json
{"errors":0,"minute":"2026-11-16T08:00:00Z","requests":100}
```

也支持标准 JSON 数组和 `{"records": [...]}` / `{"events": [...]}` 包装对象。
时间戳可为 UTC ISO-8601（必须分钟对齐）或整数 Unix 秒（必须是 60 的倍数）。

加载校验是整体拒绝语义：时间戳重复/倒退、失败数大于总数、字段类型错误、非分钟
对齐、JSON 损坏等都会抛出 `E_SLI`，`details.line` 给出物理行号；不会返回部分数据。

## 配置示例

完整配置见 `examples/config/slo.json`：

```json
{
  "schema_version": 1,
  "sli": {
    "id": "checkout-availability",
    "service": "checkout-api",
    "timestamp_field": "minute",
    "total_requests_field": "requests",
    "failed_requests_field": "errors",
    "timestamp_unit": "minute"
  },
  "slo": {
    "id": "checkout-availability-slo",
    "version": "slo-2026-11-17.1",
    "target_ppm": 999000
  },
  "windows": {"long_minutes": 60, "short_minutes": 5, "step_minutes": 5},
  "alert_policy": {
    "warning_long_ppm": 2000000,
    "warning_short_ppm": 1500000,
    "critical_long_ppm": 10000000,
    "critical_short_ppm": 5000000
  },
  "precision": {
    "ratio_scale": 1000000000,
    "burn_scale": 1000000,
    "request_scale": 1000000
  },
  "adaptive": {"row_threshold": 50000}
}
```

`target_ppm` 表示每百万请求允许的可用性目标；`target_ratio`（字符串）可替代它。
所有阈值均为精确整数 ppm，不经过二进制浮点。

## 计算语义

- 窗口锚点为第一条记录的分钟；窗口起点为 `anchor + k * step_minutes`，只要起点
  不晚于最后一条记录就生成窗口，起点和终点均与分钟边界对齐，终点为开区间。
- 长窗口与短窗口在相同评估起点成对输出。最后一个覆盖时间不足窗口长度的结果标记
  `partial=true`，`coverage_minutes` 表示实际时间覆盖，`observed_minutes` 表示实际
  记录行数；缺失中间分钟按零请求参与区间求和，但不会被伪造成记录。
- 窗口可用性 `SLI = (total - failed) / total`；错误预算比例
  `1 - target`；预算总量按当前窗口总请求数计算；剩余预算
  `total * (1 - target) - failed`。
- 燃烧率 `burn = error_rate / (1 - target)`。任何窗口的 `total=0` 时 SLI 与燃烧率
  显式标记为未定义，不伪造数值。
- 告警采用长、短窗口同时越线的组合规则：`critical` 要求两个窗口均达到 critical
  阈值；否则检查 warning 组合；否则 `normal`。状态迁移记录触发窗口 ID、两个燃烧率
  与所用 SLO 版本。

## 定点精度与误差界

内部使用 `fractions.Fraction` 精确计算，只在序列化边界做一次 round-half-even
量化。每个公开比例/燃烧率均为：

```json
{
  "defined": true,
  "ticks": 333333333,
  "scale": 1000000000,
  "value": "0.333333333",
  "error_upper_ticks": 1,
  "error_upper": "0.000000001"
}
```

误差上界取 `ceil(abs(exact_ticks - quantized_ticks))`，因此恰可表示时为 `0`，否则
保守报告 `1` tick。默认精度：SLI/错误率 `1e-9`，燃烧率 `1e-6`，请求预算
`1e-6` 个请求。公开 JSON 不输出未标定精度的浮点比例。

## 自适应路径与资源报告

`adaptive.row_threshold` 是自动路径阈值。自动模式只扫描文件一次：先缓冲至阈值，
超过阈值后把已缓冲记录送入固定容量事件环形缓冲，之后继续流式消费，不重新读取
文件。两套算法使用相同窗口、预算和量化函数，测试要求逐字段完全一致。

流式路径仅保留事件环形缓冲（容量为最长窗口分钟数 + 2）和已产生的窗口/告警状态；
窗口输出本身随评估步数增长，这是快照所需的业务结果。每次评估报告：

- `adaptive_path`：`full` 或 `streaming`；
- `peak_memory_estimate_bytes`：基于保留核心结构的确定性估算；
- `peak_memory_observed_bytes`：`tracemalloc` 在解析开始至告警构建结束的峰值；
- `elapsed_ms` / `elapsed_us`：整数耗时；
- `memory_accounting` 与 `estimator`：明确估算和观测口径。

内存估算只覆盖保留的核心结构；观测值还会包含 JSON 解析临时对象，因此通常大于
估算值。两者都排除了解释器启动和评估前已存在的对象。

## 快照、恢复与重放

`save_snapshot` 原子写入完整会话：SLI 定义、SLO 目标与版本、数据版本/哈希、窗口
参数、全部长/短窗口结果、每步告警、状态迁移和资源报告。快照中禁止浮点数。

`restore_snapshot` 总是构造与快照完全一致的新会话，`action=full_replace`、
`merged=false`。若提供当前数据版本或哈希，差异按 `data_version`、`data_sha256`
的稳定顺序列出，但不会与新窗口结果混合。

`replay_snapshot` 重新读取数据、逐窗口和逐评估步骤重算，并与快照中的整数定点值、
误差界、窗口 ID、告警状态、迁移和最终状态逐字段比较。数据版本、数据哈希、窗口
参数不一致或结果被篡改时抛出 `E_REPLAY`，其 `details` 包含
`first_mismatch_step` 和 `first_mismatch_field`。

## Python API

```python
from sre_burnrate import (
    load_config, load_sli, evaluate_file, save_snapshot,
    restore_snapshot, replay_snapshot,
)

config = load_config("examples/config/slo.json")
loaded = load_sli("examples/data/sli_minutes.ndjson", config.sli)
session = evaluate_file(
    "examples/data/sli_minutes.ndjson",
    config,
    data_version="checkout-2026-11-16-v1",
)
save_snapshot(session, "examples/out/session.snapshot.json")

restored = restore_snapshot(
    "examples/out/session.snapshot.json",
    current_data_version="checkout-2026-11-17-v2",
)
replayed = replay_snapshot(
    "examples/out/session.snapshot.json",
    data_path="examples/data/sli_minutes.ndjson",
    data_version="checkout-2026-11-16-v1",
)
assert replayed.verified
```

包还公开 `compute_burn_rate`、`compute_budget_metrics`、`FullWindowAggregator`、
`StreamingWindowAggregator`、`AlertStateMachine`、`quantize`、`FixedPoint` 等可组合 API。

## 业务错误码

| 代码 | 含义 |
| --- | --- |
| `E_SLI` | SLI 数据非法；`details.line` 为物理行号，整文件拒绝 |
| `E_CONFIG` | 配置缺失、类型错误或阈值组合非法 |
| `E_SNAPSHOT` | 快照格式/结构/固定点字段校验失败 |
| `E_REPLAY` | 重放不可复现或结果不一致；含首个不一致步骤与字段 |
| `E_INPUT` | 本地文件不可读或不可写 |

## 目录

```text
src/sre_burnrate/   领域包（加载、聚合、告警、引擎、快照、重放）
examples/           确定性样本、配置、四场景演示
tests/              32 项单元/集成测试
tools/benchmark.py  百万行规模验证脚本
docs/               设计与实际验证记录
```

更详细的设计取舍见 `docs/technical-design.md`，实测结果见 `docs/VERIFICATION.md`。
