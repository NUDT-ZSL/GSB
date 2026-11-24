# VPN 隧道转发离线诊断引擎技术说明

## 1. 运行方式

要求 Python 3.11+；代码使用 `from __future__ import annotations`，在验证环境的 Python 3.10.12 下也可运行。

```powershell
pip install -r requirements-dev.txt
python -m vpn_diag.cli generate-sample -o samples\fixed_seed_sample.json
python -m vpn_diag.cli import-sample -i samples\fixed_seed_sample.json -w .vpn_diag
python -m vpn_diag.cli run -s samples\fixed_seed_sample.json -w .vpn_diag --start-echo
python -m vpn_diag.cli progress -w .vpn_diag
python -m vpn_diag.cli export-report -w .vpn_diag -o artifacts\diagnostic-report.json
python -m vpn_diag.cli verify-checkpoint -w .vpn_diag -s samples\fixed_seed_sample.json
```

五类核心子命令为 `import-sample`、`run`、`progress`、`export-report`、`verify-checkpoint`；另外提供 `generate-sample` 与 `echo-server`。

## 2. 定点模型

当前定点标度版本为 `fp-q1000-v1`，`FIXED_SCALE=1000`：

- 时间单位为毫秒，`1 q = 0.001 ms`。
- 丢包率、抖动上限等 `[0, 1]` 比率同样映射到 `[0, 1000]`。
- 样本中的十进制值先按声明范围校验，再解析为整数 q。
- 乘法和除法使用整数 `//`，每次产生非零余数的向下取整累计 1 q 的保守误差上界。

单分片路径成本为：

```text
wire_bytes              = fragment_length + crypto_overhead
rtt_q                   = rtt_ms * 1000
loss_penalty_q          = floor(rtt_q * loss_rate_q / 1000)
queue_delay_q           = floor(wire_bytes * 8 * 1000^2 / bandwidth_kbps_q)
jitter_q                = floor(rtt_q * jitter_limit_q / 1000)
estimate_q              = link_weight * (rtt_q + loss_penalty_q + queue_delay_q + jitter_q)
rto_q                   = max(clock_step_q, estimate_q)
```

float64 参考路径使用相同运算顺序和原始十进制转成的 Python `float`（IEEE-754 binary64）。每轮同时维护定点累计成本、参考累计成本和边界余量；判定不同即写入 `divergences`。若累计误差界达到容差标度且至少一条路径的余量位于容差带内，记录稳定业务码 `E_TOLERANCE_AMBIGUOUS`，并给出两条路径的判定、余量、误差界和容差。

重传预算只通过 `peer.retransmit_used` 与 `policy.retransmit_budget` 的整数比较消耗，不使用浮点计数。

## 3. MTU 与同步标记

设：

```text
boundary = outer_mtu - crypto_overhead
upper    = fragment_threshold
```

当 `boundary <= 0`、`upper == 0` 或 `upper > boundary` 时，返回 `E_MTU_INFEASIBLE`。可行时按 `upper` 对每条内层载荷顺序切片，分片键为：

```text
<peer>:p<payload_index>:f<fragment_index>
```

同步标记为 `SYNC:<key>:<offset>:<length>`。生成器先用同一整数切分规则生成标记，引擎再次计算并与样本声明逐一比较；篡改标记会产生 `E_SYNC_MARKER_MISMATCH`。

## 4. 轮次、事件与闭包

每轮逻辑时钟从当前水位开始：

1. 应用所有 `at_clock_ms <= clock` 的事件。
2. 用切换后的画像重算在途分片 RTO。
3. 检查超时并合并注入的 `retransmit` 事件。
4. 重传分片优先，随后按顺序加入未发送分片。
5. 选择数量不超过对端并发上限，并检查轮预算。
6. 通过一个 UDP socket 批量发出，等待匹配 `(fragment_key, attempt)` 的回显。
7. 处理真实 RTT、模拟丢包段、确认、停滞和进度。
8. 完成本轮后推进 `clock_step_ms`，写入 SQLite 与快照检查点。

若首个候选分片自身越过预算，引擎仍发出该分片以保证恢复可行，但立即设置 `E_ROUND_BUDGET_TRUNCATED` 并停止该对端本轮后续选择；若已有选中分片后再越界，则后续分片延期。这对应“超出预算时截断该轮而非整体失败”。

`profile_change` 会把当时所有 in-flight 分片写入同一个画像切换闭包，保存旧/新画像、闭包 ID 和 RTO 重算结果。`loss_segment` 仍进行真实 UDP 收发，但在逻辑层丢弃该时间窗内的确认，从而确定性复现超时与重传。

## 5. UDP 协议与观测

回显服务绑定 `127.0.0.1` 临时或声明端口，使用守护 `threading.Thread` 循环 `recvfrom`/`sendto`。数据包头为：

```text
struct.Struct("!2sBBHI") = magic(2s) | version(B) | flags(B) | key_length(H) | attempt(I)
```

客户端用 `time.perf_counter()` 测量 RTT，同时记录 `time.time()` 墙钟值。实测 RTT 与定点 RTT 分量的偏差超过 `tolerance_ms` 时进入 `observation_deviations`，包含对端名和分片键。

无回显时使用 `socket.settimeout(deadline)` 真实等待；完全没有观测到确认时返回 `E_ECHO_UNREACHABLE`。收到 ICMP/连接重置等本机错误时也映射到同一业务码。

## 6. 检查点与恢复

工作区包含：

- `checkpoints.sqlite3`：`meta`、`checkpoints`、`kv` 表；检查点行保存状态 JSON 与 SHA-256。
- `snapshots/checkpoint-<watermark>-<id>.json`：原子 JSON 快照。
- `imported_sample.json`、`normalized_sample.json`：导入样本和规范化结果。

快照和报告均通过“同目录临时文件 + `flush/fsync` + `os.replace`”原子替换。恢复时从新到旧选择第一个数据库、快照文件、SHA-256 均完整的检查点，并用样本重放分片计划与键顺序。`*.tmp`、`writing` 行、缺失/哈希不符快照会进入校验结果；最新检查点损坏时回退到上一个完整检查点。

`checkpoint_schema_version` 或 `fixed_scale_version` 不一致时抛出 `E_CHECKPOINT_VERSION`；运行入口默认归档旧 SQLite/快照目录并整体重建，不做增量续接。

## 7. 固定样本与验证结果

固定种子为 `20261125`，生成文件：

- `samples/fixed_seed_sample.json`

样本包含正常传输、同步标记、定点容差边界、画像切换闭包、持续丢包停滞、MTU 不可行和多条非法记录。

实际验证命令：

```bash
python3 -m pytest -q
uv run --python 3.11 --with pytest python -m pytest -q
```

结果：

```text
Python 3.10.12: 15 passed
CPython 3.11.18 (uv): 15 passed in about 1.8s
```

两次独立固定样本运行（真实回显、不同工作区）去除墙钟观测值后的确定性逻辑摘要均为：

```text
55b5c81184f572d88bf2a8006e6b8ebff01e2a056b2472e8a5d45d4ea1d5d8c6
```

CLI 验收产物：

- `artifacts/import-summary.json`
- `artifacts/progress.json`
- `artifacts/verify.json`
- `artifacts/diagnostic-report.json`

固定样本报告摘要为 6 个受理对端、7 条拒绝记录、4 个完成、1 个重传耗尽停滞、1 个 MTU 不可行、16 个完成轮次、1 个 `E_TOLERANCE_AMBIGUOUS` 和 1 个画像切换闭包。实测 RTT 是本机调度相关量，因此不纳入确定性逻辑摘要；其偏差项和键名仍完整写入报告。
