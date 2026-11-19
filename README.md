# h2diag：本地 HTTP/2 多路复用与恢复一致性工作台

`h2diag` 是一个只依赖 Python 标准库的本地诊断工具。它不连接真实 HTTP/2
对端，而是按固定 JSON 场景和注入逻辑时钟推进帧级状态；每帧处理后把连接、
全部流、两级窗口、逻辑时钟水位、帧摘要和账本摘要作为一个 SQLite 事务提交。
进程可在任意事务中被强制终止，`resume` 会回到最后一个完整帧边界继续。

## 快速开始（Windows）

要求 Python 3.11 或更高版本：

```powershell
py -3.11 -c "import sys, sqlite3, asyncio, hashlib, json; print(sys.version); print(sqlite3.sqlite_version)"
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

项目没有第三方运行时依赖。以下命令均可用 `.\.venv\Scripts\python.exe`
代替 `py -3.11`，也可直接调用安装后的 `h2diag.exe`。

```powershell
# 固定种子生成确定性样本
py -3.11 -m h2diag generate-sample --out samples\demo_scenario.json --seed 20261120

# 干净运行并输出逐帧账本
py -3.11 -m h2diag run --scenario samples\demo_scenario.json `
  --db artifacts\runs\clean.db --run-id demo --trace artifacts\runs\clean.jsonl

# 稳定键序 JSON 状态
py -3.11 -m h2diag status --db artifacts\runs\clean.db --run-id demo

# 从场景重新推演，并逐帧比对 SQLite 快照、轨迹摘要和窗口账本
py -3.11 -m h2diag verify --scenario samples\demo_scenario.json `
  --db artifacts\runs\clean.db --run-id demo
```

一键恢复验收：

```powershell
py -3.11 scripts\crash_resume_demo.py
```

报告写入 `artifacts\runs\validation_report.json`，并同时生成干净运行和
中断恢复运行的逐帧 trace、最终状态和恢复前状态。

## 强制终止与恢复

内置故障注入可在指定帧事务内写入后立即调用 `os._exit(91)`，不执行任何
Python 清理，用于稳定复现“半提交帧不可见”的边界：

```powershell
py -3.11 -m h2diag run --scenario samples\demo_scenario.json `
  --db artifacts\runs\crash.db --run-id demo `
  --trace artifacts\runs\crash.jsonl --crash-before-commit 15

# 此时 status 必须显示 running、committed_seq=14，而不是 15
py -3.11 -m h2diag status --db artifacts\runs\crash.db --run-id demo

py -3.11 -m h2diag resume --scenario samples\demo_scenario.json `
  --db artifacts\runs\crash.db --run-id demo `
  --trace artifacts\runs\crash.jsonl

py -3.11 -m h2diag verify --scenario samples\demo_scenario.json `
  --db artifacts\runs\crash.db --run-id demo
```

也可用任务管理器或 `taskkill` 从外部终止。为稳定命中事务中间点，可让
第 15 帧事务暂停：

```powershell
$python = (Resolve-Path .\.venv\Scripts\python.exe).Path
$p = Start-Process -FilePath $python -ArgumentList @(
  "-m", "h2diag", "run",
  "--scenario", "samples\demo_scenario.json",
  "--db", "artifacts\runs\external_kill.db",
  "--run-id", "demo",
  "--pause-at-seq", "15",
  "--pause-before-commit-ms", "60000"
) -WorkingDirectory (Get-Location) -PassThru
Start-Sleep -Seconds 3
taskkill /F /PID $p.Id
```

随后执行相同的 `status`、`resume`、`verify`。SQLite 的事务回滚保证第 15
帧不会出现在快照中，恢复只从第 14 帧水位之后开始。

## 场景脚本

样本由 `h2diag/sample.py` 中固定种子的 `random.Random(seed)` 生成；随机数
只用于头部块和 PING 字节，窗口、权重、事件长度和顺序完全固定。

顶层结构：

```json
{
  "schema_version": 1,
  "scenario_id": "h2-mux-recovery-20261120",
  "initiator": "client",
  "connection": {
    "send_window": 3000,
    "recv_window": 20000,
    "max_window": 2147483647,
    "max_concurrent_streams": 8
  },
  "streams": [
    {
      "stream_id": 1,
      "initial_send_window": 12000,
      "initial_recv_window": 20000,
      "weight": 16,
      "expected_final_state": "closed"
    }
  ],
  "events": []
}
```

校验规则：

- 流 ID 严格递增且非零；`client` 发起流必须为奇数，`server` 必须为偶数。
- 初始发送/接收窗口为正整数且不超过 `max_window`。
- 权重范围为 `1..256`。
- `events[i].seq` 必须等于 `i + 1`，逻辑时钟 `at_ms` 非降序。
- 帧引用流必须已声明，否则返回 `E_UNKNOWN_STREAM`。
- `GOAWAY.last_stream_id` 必须在 `0..最大已声明流 ID` 内。
- 每个流必须声明合法的 `expected_final_state`。

所有结构错误及完整状态推演错误都在打开 SQLite 前完成检查。非法场景不会
创建数据库、trace 或运行记录；CLI 在 stderr 输出稳定 JSON 错误，包括
错误码、事件序号、路径和原因。

## 帧与方向语义

方向以本地端点视角解释：

- `inbound`：从对端收到。入站 `DATA` 消耗连接/流接收窗口；入站
  `WINDOW_UPDATE` 增加本地发送窗口。
- `outbound`：由本地发出。出站 `DATA` 消耗连接/流发送窗口；出站
  `WINDOW_UPDATE` 增加本地接收窗口。

帧类型和主要字段：

| 类型 | 字段 |
| --- | --- |
| `HEADERS` | `stream_id`, `direction`, `fragment_hex`, `end_headers`, `end_stream` |
| `CONTINUATION` | `stream_id`, `direction`, `fragment_hex`, `end_headers` |
| `DATA` | `stream_id`, `direction`, `length`, `end_stream` |
| `RST_STREAM` | `stream_id`, `direction`, `error_code` |
| `WINDOW_UPDATE` | `level=connection/stream`, `direction`, `increment`, 流级时带 `stream_id` |
| `SETTINGS` | `direction`, `ack`, 非 ACK 时带 `settings` |
| `PING` | `direction`, `ack`, `payload_hex`（必须为 8 字节） |
| `GOAWAY` | `direction`, `last_stream_id`, `error_code`, `debug_hex` |

`fragment_hex` 和 `payload_hex` 使用十六进制字符串。头部块只在完整
`END_HEADERS` 后记录 SHA-256 摘要和字节数，不实现 HPACK 语义解码。

## 核心机制

### 双层流量控制

连接和每条流分别维护发送、接收窗口。每个出站 DATA 分配量同时受
`connection.send_window`、`stream.send_window`、待发送字节和流状态约束。
入站 DATA 同时校验并扣减连接与流接收窗口。

每帧强制校验：

```text
connection.send_window
  = initial_send_window + send_window_updates
    - bytes_sent_total + reset_reclaimed

connection.recv_window
  = initial_recv_window + recv_window_updates - bytes_received_total

stream.send_window
  = initial_send_window + send_window_updates - bytes_sent

stream.recv_window
  = initial_recv_window + recv_window_updates - bytes_received
```

同时校验 `connection.unacked_send == sum(stream.unacked_conn)`，以及连接
累计发送/接收字节与所有流累计值一致。`WINDOW_UPDATE` 溢出返回
`E_WINDOW_OVERFLOW`，入站 DATA 超出任一接收窗口返回 `E_FLOW_CONTROL`。

### 确定性优先级调度

出站 DATA 事件把请求量加入流的 `pending_outbound`，随后执行全局调度：

1. 候选流必须可发送、有 pending 字节，且连接/流发送窗口都大于零。
2. 按 `(bytes_sent * 1_000_000 // weight, stream_id)` 升序选择。
3. 本轮分配
   `min(weight * 128, pending, stream.send_window, connection.send_window)`。
4. 更新窗口和累计字节，重新排序，直到无候选或无连接额度。

权重决定单轮量，历史已发送字节决定归一化服务量；固定整数运算和稳定
`stream_id` 打破平局，因此分配序列逐位可复现。

### 流状态机

支持 `idle`、`open`、`half-closed-local`、`half-closed-remote`、
`closed`、`error`。`END_STREAM` 按方向迁移；两侧都结束后进入
`closed`。终态流不再接受 DATA、HEADERS、WINDOW_UPDATE 或新的
RST_STREAM，因此已关闭流不会被复活。非法迁移返回 `E_STREAM_STATE`。

### HEADERS/CONTINUATION

HEADERS 未置 `END_HEADERS` 时开启连接级重组状态。后续帧必须是同流、
同方向的 CONTINUATION；其他帧、错误流/方向或孤立 CONTINUATION 返回
`E_HEADERS_SEQUENCE`。场景结束时仍有未闭合头部块返回
`E_HEADERS_INCOMPLETE`。

### RST_STREAM 与 GOAWAY

- 合法 RST_STREAM 立即关闭流、丢弃 pending 数据，并把该流尚未被连接级
  WINDOW_UPDATE 确认的发送字节归还连接发送窗口；归还量受
  `max_window` 限制。该流的窗口额度之后不能再被占用。
- 连接级 WINDOW_UPDATE 的确认量按 `(first_unacked_seq, stream_id)` 稳定
  顺序分摊到各流，保证流级与连接级未确认账本始终相等。
- GOAWAY 后，已声明但尚未打开的流若 ID 大于 `last_stream_id`，打开操作
  返回 `E_GOAWAY_STREAM`；已活动且大于该水位的流进入 `error`，并归还
  未确认连接额度；不大于水位的存活流仍可推进到终态。

### SETTINGS 与 PING

SETTINGS ACK 必须匹配对应方向尚未确认的 SETTINGS；PING ACK 必须匹配
最早未确认的同 payload PING，否则分别返回 `E_SETTINGS_STATE` 和
`E_PING_STATE`。未确认的 SETTINGS/PING 状态也会持久化。

## SQLite 事务与恢复

数据库启用 WAL 和 `synchronous=FULL`。每个逻辑帧在一个
`BEGIN IMMEDIATE` 事务内同时写入：

- `snapshots`：帧序号、逻辑时钟、完整状态 JSON、帧摘要、状态摘要、
  窗口账本 JSON、账本摘要、轨迹链摘要。
- `runs`：新的 `committed_seq` 和最新摘要。

`runs.committed_seq` 是唯一提交水位。事务未提交时，快照行和 run 水位都不
可见；进程被 `os._exit`、任务管理器或 `taskkill /F` 终止后，SQLite 回滚
半提交事务。读取 `status` 时使用单个只读事务，只会看到完整帧边界。

摘要在纯内存推演和持久化路径中使用相同算法：

```text
state_digest      = SHA256(canonical(state))
ledger_digest     = chain(previous_ledger, canonical(window_ledger))
trajectory_digest = chain(previous_trajectory,
                          frame_digest, state_digest, ledger_digest)
```

`resume` 校验场景摘要后，从最后快照重建 `EngineState`，跳过所有
`seq <= committed_seq` 的事件，只推进水位之后的事件。重复调用 `resume`
对一个已完成 run 是无操作，不会再次迁移状态或回收额度。

`verify` 从相同场景做全新推演，逐帧比较持久化的
`state_json/state_digest/ledger_digest/event_digest/trajectory_digest`，
并检查快照序号无缺口、无额外行、`PRAGMA integrity_check` 为 `ok`。

## 错误码

| 错误码 | 含义 |
| --- | --- |
| `E_BAD_SCENARIO` | 场景结构、ID、权重、时钟或窗口声明非法 |
| `E_UNKNOWN_STREAM` | 帧引用未声明流 |
| `E_STREAM_STATE` | 当前流状态不允许该帧或迁移 |
| `E_FLOW_CONTROL` | DATA 超出发送/接收额度或账本不一致 |
| `E_WINDOW_OVERFLOW` | WINDOW_UPDATE 超过上限 |
| `E_HEADERS_SEQUENCE` | HEADERS/CONTINUATION 顺序或配对错误 |
| `E_HEADERS_INCOMPLETE` | 场景结束时头部块缺少 END_HEADERS |
| `E_GOAWAY_STREAM` | GOAWAY 后打开超过 `last_stream_id` 的新流 |
| `E_SETTINGS_STATE` | SETTINGS ACK 无匹配项 |
| `E_PING_STATE` | PING ACK 无匹配项或 payload 不符 |
| `E_FINAL_STATE` | 推演终态与流声明不一致 |
| `E_SCENARIO_MISMATCH` | resume/verify 使用的场景摘要不同 |
| `E_TRAJECTORY_MISMATCH` | 快照、账本或轨迹摘要无法重放 |
| `E_INCOMPLETE_RUN` | 未 resume 完成就执行 verify |

## 实测结果

本仓库在 Python 3.11.15、SQLite 3.53.1 下执行：

```text
Ran 18 tests in 0.733s
OK
```

固定种子样本包含 5 条流、26 个帧事件。强制在第 15 帧事务内写完后退出，
退出码为 91，恢复前：

```text
status        = running
committed_seq = 14
```

`resume` 和 `verify` 后，干净运行与中断恢复运行完全一致：

```text
final_state_digest      = 32fac4cddeda1b4fc199e2b6e0e67e8b59ab2125c0a21fb4c6e72a369d88289a
final_trajectory_digest = 4c8a443e57a4ecad887ad2ed0d26a979a268b9368bafccb48eb71bb4584f0b39
final_ledger_digest     = bdd69c2925700318b6feca7fc6e0ee5e78b1c3d4b836c8e43a42860e38299c88
status_json_byte_identical   = true
frame_trace_byte_identical   = true
trace_lines                  = 26 / 26
integrity_check              = ok
```

完整报告和证据文件：

- `artifacts/runs/validation_report.json`
- `artifacts/runs/demo_clean.trace.jsonl`
- `artifacts/runs/demo_crash.trace.jsonl`
- `artifacts/runs/demo_final_status.json`
- `artifacts/runs/demo_before_resume_status.json`

本次实测运行环境为 WSL2 Linux；实现只使用跨平台标准库和 SQLite 文件锁，
没有 Unix 专属调用，Windows 上使用上文 PowerShell 命令执行等价流程。

## 项目结构

```text
h2diag/
  scenario.py   场景解析与业务校验
  engine.py     状态机、双层窗口、调度、头部重组、摘要账本
  runner.py     纯内存预演、逐步提交、resume、verify
  storage.py    SQLite schema、事务、快照和只读状态
  sample.py     固定种子确定性样本
  cli.py        run/resume/status/verify/generate-sample
tests/
  test_workbench.py
scripts/
  generate_sample.py
  crash_resume_demo.py
samples/
  demo_scenario.json
artifacts/runs/
  validation_report.json
```

运行全部测试：

```powershell
py -3.11 -m unittest discover -s tests -v
```

