# 网关限流与鉴权中间件 — 技术说明

## 1. 范围与运行方式

- 纯本地、离线、确定性组件：无容器、无外部网络依赖；核心仅使用 Python 3.11+
  标准库（含 `sqlite3`）。NumPy 与 Tkinter 为可选依赖，缺失时自动降级。
- 输入为工作区内本地文件：路由定义 JSON、令牌表 JSON、请求序列 NDJSON。
- 两种入口：
  - 批处理 CLI：`python -m gateway run --routes ... --tokens ... --requests ... --workdir ...`
  - 回环 TCP 服务：`python -m gateway serve --port <高位端口> ...`，JSON 行协议，
    每行一个请求对象，每行返回一个响应对象。
- 固定产物目录：`workdir/<实验名>/ledger.db`（SQLite 账本）、
  `workdir/<实验名>/snapshots/*.json`（轨迹/状态/一致性/选择/版本汇总）、
  `reports/demo_report.json`（可复现报告）。

## 2. 稳定业务码

| 代码 | 含义 |
| --- | --- |
| `E_OK` | 鉴权通过且所选限流路径放行 |
| `E_SPEC` | 路由/令牌表/请求流强校验失败（整体拒绝加载，带文件与行号） |
| `E_AUTH` | 鉴权失败（令牌不存在、绑定不符、过期、签名不符等） |
| `E_LIMIT` | 鉴权通过但被限流；零窗口参数注入也归并到此码 |
| `E_VERSION` | 策略/令牌表版本非单调递增（版本冲突） |
| `E_REPLAY` | 恢复后重放与账本记录逐字段比对出现首个不一致 |
| `E_UNKNOWN` | 强制终止遗留的未完成批次，必须重跑，不产出退出报告 |

## 3. 强校验（E_SPEC）

`gateway/loader.py` 在加载期整体校验，任一失败即拒绝整份输入：

- 路由名唯一；路径前缀必须以 `/` 开头且不含空白。
- `rate_per_sec/burst/window_ms/max_in_window` 均为正整数，`window_ms > 0`。
- 令牌字符串全局唯一；`expires_at_ms` 为非负整数。
- 请求 NDJSON：`seq` 唯一正整数、路由必须存在、`ts_ms` 非负且全局单调不减。
- JSON 语法错误直接使用解析器给出的行号；语义错误通过对源文本做键定位
  （`_locate_line`）输出文件与行号。

## 4. 鉴权与带版本缓存

`gateway/auth.py`：

- 令牌策略：校验令牌存在性、客户端绑定、有效期（`ts_ms > expires_at_ms` 即过期）。
- 签名策略：HMAC-SHA256，载荷
  `key_id|client_id|route|signed_at_ms|ts_ms`，校验密钥存在、绑定、有效期、
  签名时间不得在未来、时钟偏斜上限 300000ms、摘要常量时间比较。
- 缓存键包含全部凭据字段（路由、客户端、令牌、签名、签名时间），篡改签名无法
  命中旧的成功缓存；仅成功结果写缓存，命中时校验条目的策略版本
  `(routes_version, token_table_version)`。
- 版本不符时不做逐条修补：策略演进调用 `replace_policy` 整体清空缓存并重建，
  返回被清空条目数；`verify_direct` 提供无缓存参考路径，测试保证两者结论一致。
- 鉴权失败的请求不会推进任何限流器（不消耗配额），三条路径统一标记
  `skipped_after_auth_failure`。

## 5. 三条限流路径与整数语义（gateway/limiters.py）

三条路径对同一请求序列独立推进，互不共享状态。所有运算均为整数运算。

### 5.1 令牌桶 TokenBucket
- 状态：整数令牌数、上次产生整数令牌的时间锚点。
- 补充量 `added = (elapsed_ms * rate_per_sec) // 1000`，**向下取整
  （floor，对非负数等价于向零截断）**；不足 1 个令牌的零头不推进时间锚点，
  在后续请求中继续累计，避免零头被静默丢弃。
- 令牌数永不超过突发容量 `burst`；`tokens >= 1` 放行并扣 1，否则拒绝。

### 5.2 滑动窗口日志 SlidingWindowLog
- 保留已放行时间戳的有序日志，窗口为半开区间 `(t-window_ms, t]`：
  年龄恰好等于窗口长度的边界时间戳滑出。
- 滑出后活跃数 `< max_in_window` 则追加并放行，否则拒绝；剩余配额
  `max_in_window - len(log)`。

### 5.3 滑动窗口计数 SlidingWindowCounter
- 以 epoch 对齐的整数桶 `ts // window_ms` 计数，仅保留当前桶与上一桶。
- 加权估计 `current + (prev * (window-offset) // window)`，权重乘法后
  **向下取整**，半个请求以下的部分被截断。
- 估计值 `< max_in_window` 则当前桶 +1 放行，否则拒绝。
- 构造器显式拒绝 `window_ms <= 0`，零窗口注入由此产生 `E_LIMIT`。

每条路径输出逐请求 `allow/remaining/reason/detail`，并提供
`snapshot/restore` 用于账本持久化与恢复。

## 6. 一致性比较与边界归因（gateway/consistency.py）

- 仅对鉴权通过的请求比较三条路径的放行/拒绝；全一致输出一致性摘要。
- 不一致时按 `seq` 排序，输出差异时刻、三条路径的剩余配额与确定性边界类别：
  - `refill_truncation`：令牌桶 floor 补充丢零头 vs 日志的整窗口精确计数。
  - `window_alignment`：epoch 对齐加权计数对精确窗口的近似（offset 驱动）。
  - `log_retention_span`：边界时间戳的日志保留跨度与桶容量语义差异。
- 归因是纯函数 `classify_boundary(decisions, window_ms, ts_ms)`，同输入同输出；
  测试通过双客户端交错重排验证“同一相对请求无论落在序列何处，归因结论相同”。

## 7. 自适应路径选择（gateway/selector.py）

- 规模输入：客户端基数、时间跨度、窗口长度、窗口内最大请求数、观测到的最大
  日志保留长度。
- 三条路径给出确定性的每秒工作量估计（常数/桶数/日志长度三类公式）。
- 显式阈值 `WORKLOAD_EPSILON = 500.0`：候选工作量之差小于该值进入稳定决胜，
  按固定优先级 `token_bucket < sliding_window_counter < sliding_window_log`
  选择，绝不依赖字典或哈希遍历顺序。
- 极端规模规则：当日志保留量触顶且客户端基数 >= 100 时，结构性淘汰日志路径。
- NumPy 仅用于向量化交叉校验，标量估计为权威结果，两者必须一致（有测试）。
- 引擎记录 `selected_path`，有效决策即取自该路径，保证选择与实际轨迹对应。

## 8. 策略版本演进与整体替换（gateway/engine.py）

- 版本号为 `(routes_version, token_table_version)`，只允许严格单调递增，
  回滚由加载/账本层报 `E_VERSION`。
- 演进时旧 `VersionState`（路由、限流器集合、鉴权器、轨迹）整体归档，新状态从
  空集合派生，不做增量合并；输出受影响路由（按字段差异比较）与清空缓存条目数。
- `versioned_summary` 把非当前版本的决策标记 `stale` 并排除出当前汇总，
  仅保留审计用途。

## 9. SQLite 账本、强制终止与恢复（gateway/ledger.py, gateway/replay.py）

表：`meta / policy_snapshots / batches / requests / structured_log / replay_results`。

- 每个批次先 `begin_batch(status=open)`，处理完整体
  `commit_batch(status=complete)`，写入批次决策、各路径状态摘要、鉴权缓存版本、
  策略版本与完整请求/决策 JSON；策略快照独立持久化以支持离线重放。
- 重启时 `mark_open_batches_unknown` 把所有 `open` 批次置为 `E_UNKNOWN`：
  只承认完整批次，未完成批次要求重跑，且不生成退出报告。
- 恢复后从最近完整批次的状态摘要重建限流器，进程可继续接受新请求。
- 重放按策略版本分组：每组用持久化策略快照从空引擎重建，按批次顺序重放，
  逐字段（顶层 decision、auth.code、三条路径的 allow/remaining/reason）比对，
  首个不一致即报 `E_REPLAY`，定位到批次、请求序号、路径与字段。

## 10. 异常注入（gateway/injection.py）

注入只修改输入文件或参数，不改写任何代码分支：

| 注入 | 手段 | 业务码 |
| --- | --- | --- |
| `corrupt_policy` | 向路由 JSON 追加非法文本，加载失败 | `E_SPEC` |
| `token_version_conflict` | 令牌表版本相对活动版本回滚 | `E_VERSION` |
| `zero_window` | 参数 `window_ms=0`，限流器构造拒绝 | `E_LIMIT` |

事件同时写入 `structured_log` 表与报告；注入后自动从备份还原输入。

## 11. 复现步骤（Windows）

```bat
python tools\env_check.py
python tools\make_samples.py
scripts\run_tests.cmd
scripts\clean.cmd
scripts\run_demo.cmd
REM 单独演示 TCP（两个终端）
scripts\run_server.cmd 18901 workdir\tcp_manual
scripts\run_client.cmd 18901 workdir\tcp_manual\responses.json
REM 注入
python -m gateway inject --kind corrupt_policy --routes samples\basic\routes.json --tokens samples\basic\tokens.json --workdir workdir\inject
```

WSL/Linux 下用 `python3` 与 `python3 tools/demo.py` 等价执行。
