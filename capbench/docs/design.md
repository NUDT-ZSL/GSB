# capbench 技术说明：观测口径、时钟基准与真值来源

## 1. 时钟基准（clock basis）
- 基准：负载驱动进程内的 `time.monotonic_ns()`，写入字段
  `clock_basis = "monotonic_ns->integer_ms"`。
- 单位：整数毫秒，由纳秒整除 1_000_000 截断得到；绝不使用墙上时钟
  （wall clock）测量延迟或时长。墙上时间仅用于快照文件名级别的
  `created_at_wall` 展示字段，不参与任何指标计算。
- 采样：独立采样线程按 `sample_interval_ms` 固定节拍唤醒，每次冻结
  区间计数并调用 `MonotonicClock.now_ms()` 打时间戳。
- 回拨检测：`now_ms()` 维护上次读数，新读数小于旧读数即抛 `E_CLOCK`，
  携带 `previous_ms / observed_ms / delta_ms`。回拨只能通过 CLI 参数
  `--inject-clock-rollback --clock-rollback-ms N` 注入，源码路径不变。

## 2. 真值来源（truth source）
- 成功判定只信任本地模拟服务返回的 JSON 回执（协议 `recv-v1`）：
  可解析且 `status == "ok"` 才计成功；传输错误、解析错误、
  `status == "error"` 一律计失败。禁止仅凭客户端“请求发出去了”推断成功。
- 服务端对每个处理完的请求同步写一行 JSON 到服务端日志
  （`workspace*/logs/server.log.jsonl` 或快照配置中的 `log_path`），
  与回执内容一致，作为可独立审计的地面真值。集成测试断言
  服务端日志的 ok/error 总数与快照内计数完全相等。
- 回执字段：`proto / req_id / status / process_ms / server_seq /
  server_time_ms / error_rate_bp`。

## 3. 确定性错误规则
服务端不使用 RNG：
`h = (server_seq * 1103515245 + 12345 + seed) % 10000`，
当 `h < error_rate_bp` 时回执 `status="error"`。
固定 `seed` 下错误序列确定，可重放；客户端调度抖动只影响请求总数，
不影响错误判定规则本身。

## 4. 定点指标与量化误差上界（聚合口径 agg-v1）
| 指标 | 表示 | 公式 | 量化误差上界 |
|---|---|---|---|
| 成功率 | 整数基点 `success_rate_bp`（1bp=0.01%） | `floor(10000*success/total)` | 向下截断误差 < 1bp |
| P95 延迟 | 整数毫秒 `p95_latency_ms` | nearest-rank，秩 `r=ceil(0.95n)`，取排序后第 r 个 | 延迟本身为整数毫秒，相对真实值误差 <= 1ms；分位取秩无插值误差 |
| 吞吐 | 整数 rps `throughput_rps` | `floor(total / (duration_ms/1000))` | 向下取整误差 < 1 rps；时长两端整数截断合计 +-1ms |

误差上界以字符串形式逐指标写入每个阶段结果的
`metrics.quantization_error_bounds`，聚合口径版本写入
`metrics.aggregation_version = "agg-v1"`。

## 5. SLO 判定与饱和原因
- `success_rate_bp < slo.min_success_rate_bp` → 失败率超限。
- `p95_latency_ms > slo.max_p95_latency_ms` → 延迟超限。
- 两类同时命中 → `both_failure_rate_and_latency`。
- 触发指标的观测值与门槛写入 `triggered_metrics`；未触发为 `pass`。

## 6. 负载参数版本与陈旧识别
- 指纹输入仅包含影响负载或判定的参数：目标地址、有序阶段列表
  （序号/时长/并发/吞吐上限/顺序）、SLO 门槛；采样间隔与展示名排除。
- 规范化 JSON（sort_keys、紧凑分隔符）后 SHA-256 取前 16 字符。
- 版本库 `state/versions.json` 持久化指纹→单调递增整数版本；
  新指纹版本号 = 现有最大版本 + 1。
- 同名快照重跑时，若指纹不一致，旧阶段结果整体标记为陈旧并整体替换
  （不是逐阶段合并），替换报告稳定列出被替换阶段序号、旧/新版本及
  字段级差异（如 `stage[2].concurrency:4->8`）。

## 7. 快照与强制终止恢复
- 快照格式 `snapshot-v1`，原子写入（tmp + os.replace）。
- 内容：场景定义、参数版本/指纹、服务端配置、每个已结束阶段的
  观测+指标+判定、进度标记、会话状态。
- 强制终止：进行中阶段只写 `unknown` 进度标记（含原因），不产生
  退出报告；恢复后 `recovery_plan` 明确列出 finished / unknown /
  必须重跑的阶段，禁止补造任何未完成阶段报告。

## 8. 重放一致性校验（固定容差）
容差常量集中在 `capbench/replay.py`：
- 成功率 +-50bp；P95 +-5ms；
- 吞吐偏差不超过 `max(3 rps, 15% 相对值)`；
- 判定结论必须精确一致。
先校验服务端配置（process_ms/error_rate_bp/seed/host/port）与
参数指纹；任一不一致或逐阶段比对超差，抛 `E_REPLAY` 并给出
首个偏差阶段 `first_deviating_stage`。

## 9. 业务码
| 码 | 触发 | 注入方式（仅改输入/启动参数） |
|---|---|---|
| E_SCENE | 场景 JSON 损坏或强校验失败，含字段路径 | `--inject-corrupt-scene` 或直接给非法文件 |
| E_PORT | 回环端口被占用/无法绑定 | `--inject-port-busy` 预占端口 |
| E_CLOCK | 单调时钟回拨 | `--inject-clock-rollback` |
| E_REPLAY | 重放不可复现或超差 | 改服务端配置/快照后执行 replay |

所有业务错误以 JSON 输出并追加到 `logs/events.jsonl` 结构化日志，
CLI 退出码为 2。
