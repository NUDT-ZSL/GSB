# 合成探测归因工具使用说明

仅依赖 Python 3 标准库，本地命令行运行，所有输出确定性可复现。

## 命令

```bash
# 1. 以固定种子生成服务目录、探测策略、合成探测记录与真值标注
python3 main.py generate --seed 42 --dir out

# 2. 聚合、事件判定、根因归因并生成报告（写回同一目录）
python3 main.py analyze --dir out

# 3. 端到端验收（独立计算器核对 + 真值比对 + 负向用例）
python3 main.py verify --seed 42 --dir verify_out
```

## 生成文件

- `catalog.json`：服务目录（服务、接口、依赖关系、责任团队）。
- `strategy.json`：探测策略与判定参数，含 `parameter_version`。
- `probes.jsonl`：探测记录，每行一条，字段为
  `tick / service / interface / node / status / latency_ms / failure_reason`。
- `ground_truth.json`：注入故障真值与期望事件清单。
- `aggregates.jsonl`：每个（服务， 接口， 时刻）的滑动窗口统计
  （探测次数、失败次数、失败率、P50/P95/P99）。
- `report.json`：归因报告（事件时间线、证据区间、根因候选排序、
  影响传播、团队摘要、输入摘要与 `config_fingerprint`）。

## 统计口径

- 滑动窗口长度取 `strategy.window_length`，按（服务， 接口）独立聚合，不跨服务混合。
- 分位数采用线性插值法（与 `statistics.quantiles(method="inclusive")` 一致）。
- 持续故障：窗口失败率 ≥ `failure_rate_threshold` 且连续 ≥ `consecutive_failure_threshold` 个时刻。
- 延迟下降：基于成功探测（status 200）耗时的窗口分位值 ≥ `latency_threshold_ms`
  且连续 ≥ `latency_consecutive_threshold` 个时刻（避免超时失败污染延迟口径）。
- 偶发失败：存在孤立失败但窗口失败率低于阈值、且不被任何事件覆盖的时刻。
- 根因打分：`time_overlap × 窗口重叠度 + dependency_proximity × 依赖接近度
  + failure_mode_match × 失败模式匹配`，若候选自身可被更上游候选解释则施加惩罚降权。
- 影响传播：从首位根因候选的服务沿依赖图反向 BFS，距离 1 为直接受影响，≥2 为间接。
- 报告指纹：`config_fingerprint` 为策略规范化 JSON 的 SHA-256，
  参数或版本变化时指纹随之变化；相同输入重算报告逐字节一致。

## 结构化错误码（stderr，退出码 2，拒绝整批且不产生部分结果）

- `E_MISSING_FIELD`：探测记录缺失必需字段。
- `E_TS_OUT_OF_ORDER`：同一（服务， 接口）时间戳乱序。
- `E_UNKNOWN_SERVICE` / `E_UNKNOWN_INTERFACE`：记录不在服务目录中。
- `E_THRESHOLD_EXCEEDS_WINDOW`：连续阈值超过窗口长度。
- `E_INVALID_RECORD` / `E_INVALID_JSON` / `E_STRATEGY_INVALID` /
  `E_CATALOG_INVALID` / `E_INPUT_NOT_FOUND`：其他输入非法。
