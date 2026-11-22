# HTTP/2 多路复用连接诊断工作台

这是一个完全本地、确定性的 HTTP/2 帧级诊断工作台。它不连接真实 HTTP/2 对端，不使用证书或外部站点；场景、增量补丁和帧事件均来自本地 JSON。FastAPI 仅可绑定回环地址。

工作台重点验证四类一致性：

- 连接级与流级双层流量控制、流状态机、HEADERS/CONTINUATION 重组和加权调度。
- 补丁修改权重、SETTINGS 初始窗口或流结束标记时，精确的依赖失效闭包与安全复用范围。
- 每帧、每轮补丁的 SQLite 原子提交，以及进程被强制终止后的仅持久化状态恢复。
- 多进程对同一连接的幂等提交、并发窗口安全和稳定查询快照。

## 环境与安装

要求 Python 3.11+。SQLite 使用标准库 `sqlite3`，无需数据库服务。

Windows PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\scripts\bootstrap.ps1
```

Linux/WSL：

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python scripts/generate_samples.py samples
.venv/bin/python scripts/validate_samples.py samples
.venv/bin/python -m pytest -q
```

依赖：`fastapi`、`uvicorn`、`numpy`、`pytest`、`httpx`。生成和验证样本本身不联网。

## 快速验收

运行完整本地演示并写入一致性报告：

```bash
.venv/bin/python -m h2diag demo \
  --db artifacts/demo.db \
  --samples samples \
  --report artifacts/consistency-report.json
.venv/bin/python scripts/run_acceptance.py artifacts/acceptance-report.json
```

演示执行：

1. 初始化 3 条并发流的场景。
2. 提交前 12 帧。
3. 应用权重和 SETTINGS 初始窗口补丁。
4. 恢复并提交余下 6 帧。
5. 用从零重算路径和逐帧快照校验最终状态。
6. 对结束标记补丁场景执行同样校验。

输出文件为 `artifacts/consistency-report.json`，包含连接窗口、每流窗口/状态/名次、补丁失效清单、复用摘要和 `verify` 报告。

## CLI

```bash
python -m h2diag generate-samples --out-dir samples
python -m h2diag init --db work.db --scenario samples/scenario.json
python -m h2diag run --db work.db --connection-id demo-connection --max-frames 12
python -m h2diag patch --db work.db --patch samples/patches/settings-weight-v2.json
python -m h2diag resume --db work.db --connection-id demo-connection
python -m h2diag status --db work.db --connection-id demo-connection
python -m h2diag verify --db work.db --connection-id demo-connection
python -m h2diag serve --db work.db --host 127.0.0.1 --port 8765
```

`status` 和所有 API 响应使用排序 JSON。`verify` 成功返回 0，发现分歧返回 1；CLI 业务错误返回 2。

## FastAPI

`python -m h2diag serve` 只允许 `127.0.0.1`、`::1` 或 `localhost`。主要端点：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/run` | 接收场景并初始化/推进帧，或按 `connection_id` 推进 |
| POST | `/patch` | 提交增量补丁 |
| POST | `/resume` | 放弃半提交意图并只从已提交水位继续 |
| GET | `/status` | 返回稳定排序的连接、流、窗口、调度与逻辑时钟状态 |
| POST/GET | `/verify` | 持久化状态与逐帧重算、补丁失效清单校验 |
| POST | `/validate/scenario` | 只校验，不写数据库 |
| POST | `/validate/patch` | 只校验摘要和结构，不写数据库 |
| GET | `/health` | SQLite 版本、WAL 与临时表支持情况 |

请求示例：

```json
{"scenario": {"format": "h2diag.scenario/v1", "...": "..."}, "max_frames": 12}
```

```json
{"connection_id": "demo-connection", "patch": {"format": "h2diag.patch/v1", "...": "..."}}
```

## 场景契约

样本见 `samples/scenario.json`。核心结构：

```json
{
  "format": "h2diag.scenario/v1",
  "scenario_id": "demo",
  "connection_id": "demo-connection",
  "local_role": "client",
  "connection": {
    "initial_send_window": 600,
    "initial_recv_window": 600,
    "max_send_window": 4096,
    "max_recv_window": 4096,
    "settings": {"initial_window_size": 240, "max_frame_size": 16384}
  },
  "streams": [
    {
      "stream_id": 1,
      "initiator": "client",
      "initial_send_window": 240,
      "initial_recv_window": 240,
      "weight": 32,
      "expected_final_state": "closed"
    }
  ],
  "events": []
}
```

约束：

- 流 ID 严格递增，奇数对应用客户端发起、偶数对应服务端发起。
- 初始窗口为正整数；权重范围 `1..256`；逻辑时钟非降序。
- `seq` 从 1 开始连续递增。
- `CONTINUATION` 必须是未完成 HEADERS 的紧邻下一帧，且流和方向一致。
- `WINDOW_UPDATE` 增量非负，应用后不得超过该层上限。
- GOAWAY 最后流 ID 不得超过声明流最大值。
- 声明了 `expected_final_state` 时，完整场景模拟必须匹配。

事件方向：

- `HEADERS`、`DATA`：`send` 表示本地发往对端，`recv` 表示对端发往本地。
- `WINDOW_UPDATE` 的 `recv` 增加本地发送额度；`send` 增加本地接收额度。
- `SETTINGS` 的 `recv` 调整本地发送窗口；`send` 调整本地接收窗口。
- `SETTINGS_ACK`、`PING_ACK` 必须匹配对应待确认帧。

头部块使用原始字节的可逆编码：`fragment_hex`，或 `fragment` + `fragment_encoding`（`utf-8`/`hex`/`base64`）。重组摘要为完整拼接字节的 SHA-256，`block_hex` 保留直接拼接结果，因此可独立复现。

## 补丁契约

补丁摘要为补丁对象（排除 `content_digest`）的规范化 SHA-256。支持操作：

- `set_weight`：只影响此后调度；已提交 DATA 的在途顺序和权重观测不被重写。
- `set_settings`：调整现有活动流对应方向窗口；任一窗口越界即拒绝整个补丁。
- `set_stream_end`：在补丁逻辑时钟处施加单调的本地/远端结束标记。
- `set_expected_final_state`：修改校验契约。
- `window_update`：在补丁边界施加窗口增量。
- `append_events`：追加新帧；必须保持 seq、逻辑时钟和头部块连续性。

补丁分类：

- `append`：配置不变，只追加事件。
- `incremental`：现有配置/窗口/结束标记变化。
- `replace`：完整场景替换，且已提交事件前缀必须一致；运行状态从头重算，并建立新 epoch。
- `noop`：有效内容与当前场景相同；不推进版本、不写运行状态。

`base_version` 必须等于当前版本；`target_version` 必须更大。目标版本小于当前版本返回 `PATCH_VERSION_001`。相同目标版本和相同摘要幂等返回，不重复应用。

## 状态与账目

每条流维护 `idle/open/half-closed-local/half-closed-remote/closed`。HEADERS 打开流，END_STREAM 按方向推进，RST_STREAM 关闭并标记 reset，GOAWAY 关闭最后流 ID 之上的流。

双层窗口：

- DATA 发送同时扣减连接发送窗口和流发送窗口，接收同理。
- WINDOW_UPDATE 只增加对应层额度，并受该层上限约束。
- RST_STREAM 将该流尚未确认的发送/接收在途额度回收到连接级，随后把该流两级窗口置零；reset 流不能再次占用额度。
- 所有当前额度始终非负，发送前同时满足 `length <= min(流发送窗口, 连接发送窗口)`。

调度：

- `scheduler.ranks` 按权重降序、流 ID 升序给出可发送流稳定名次。
- `scheduler.plan` 是确定性的 smooth weighted round-robin 未来调度预览。
- `scheduler.committed_order` 是不可改写的已提交 DATA 顺序；权重补丁不重排或丢弃它。

## 失效闭包

补丁提交前会计算并持久化：

- `affected_streams`：直接受影响的流。
- `recomputed/invalidated`：需要重新计算的字段路径。
- `changed`：补丁前后实际变化的字段路径。
- `reused_stream_digests`：未受影响流的账目、状态和头部摘要复用指纹。
- `header_decisions`：每个流头部结论是复用还是需重新裁决。

典型依赖：

```text
SETTINGS -> connection.flow_capacity -> stream windows -> scheduler ranks
stream.weight -> scheduler ranks -> future dispatch only
stream END marker -> stream state -> scheduler ranks -> header reuse decision
committed DATA -> immutable in-flight order
```

SETTINGS 补丁会显式把连接窗口纳入重算观测，但不改变已提交数据；未受影响的流窗口和头部块摘要逐字段保留。

## 原子持久化与恢复

SQLite 使用 WAL、`synchronous=FULL` 和显式 `BEGIN IMMEDIATE`。主要表：

- `connections`：当前场景版本、epoch、已提交帧水位、逻辑时钟、完整状态和摘要。
- `scenario_versions`：版本化完整场景，当前场景通过单行指针整体替换。
- `state_commits`：初始化、每帧和每次补丁后的完整状态快照。
- `frames`：帧幂等键、事件摘要和结果摘要。
- `patch_commits`：补丁前后状态、操作、失效清单。
- `commit_intents`：事务前意图；恢复时识别并标记 `abandoned` 半提交尝试。

每帧在一个事务中写入帧记录、完整状态快照和当前指针。补丁先在连接级临时表 `stage_events`、`stage_streams` 中暂存并与版本号核对，再在事务内插入版本、快照和补丁清单，最后更新单行当前指针；中途终止由 SQLite 回滚。

恢复只读取已提交水位之后的事件，不读取墙钟、不猜测半提交结果。崩溃测试使用：

```bash
H2DIAG_CRASH_POINT=frame_mid_tx H2DIAG_CRASH_SEQ=5 \
  python -m h2diag run --db work.db --connection-id demo-connection

H2DIAG_CRASH_POINT=patch_mid_tx \
  python -m h2diag patch --db work.db --patch samples/patches/settings-weight-v2.json
```

该注入通过 `os._exit(86)` 模拟强制终止，不保证任何退出报告。

## 并发与幂等

- 写操作使用跨平台文件锁：Windows `msvcrt.locking`，Linux/macOS `fcntl.flock`。
- SQLite `BEGIN IMMEDIATE` 与 WAL 保证读者只看到完整提交快照。
- 帧唯一键包含连接、补丁版本、全局帧序号、流序号和事件摘要。
- 相同帧重复提交只返回既有结果；同键不同内容返回 `IDEMPOTENCY_001`。
- 相同补丁目标版本和摘要重复提交幂等；不同摘要返回 `PATCH_CONFLICT_001`。
- 连接窗口、流窗口和调度名次在同一事务中更新，不会被两个实例同时重写。

多进程实例可以同时推进同一连接；全局帧边界由文件锁和 SQLite 串行化提交，因此结果与单进程一致。

## 校验报告

`verify` 从 epoch 场景开始，沿版本时间线重放：

- 每个已提交帧与 `state_commits` 中的持久化快照逐字段比较。
- 每个补丁边界与 `patch_commits.after_state_json` 比较。
- 检查所有实际变化路径都在失效清单内。
- 检查未受影响流的复用摘要和头部摘要。
- 检查最终状态与持久化状态完全一致。

发现分歧时输出首个分歧帧号、字段路径、持久化值、重算值和观测来源。`artifacts/consistency-report.json` 是实际验收输出。

## 实际验证结果

本次交付在 Python 3.12.13、SQLite 3.53.1 环境执行。当前容器为 Linux/WSL；Windows 路径使用标准库 `msvcrt` 实现，未在本容器内执行原生 Windows 进程测试。

- `pytest`：30 项通过。
- 主场景：18 帧、3 条并发流；窗口、状态、头部摘要和补丁闭包全部通过 `verify`。
- 帧强制终止：在水位 4 中断，恢复仅重放 14 帧，最终摘要与不中断运行一致。
- 补丁强制终止：中断后版本仍为 1，重试补丁并恢复后与不中断运行摘要一致。
- 四进程并发：帧表 18 个唯一序号、18 个逐帧快照，无重复扣减，最终 `verify` 通过。
- 最终一致性报告：`artifacts/consistency-report.json`。
- 一键验收摘要：`artifacts/acceptance-report.json`。

## 目录

```text
h2diag/             核心引擎、校验、补丁、SQLite、服务、API、CLI
samples/            确定性场景、补丁、帧事件和摘要清单
scripts/            安装、样本生成/校验脚本
tests/              单元、集成、故障注入与多进程测试
artifacts/          实际运行的一致性报告
docs/               错误码等技术说明
```

## 设计边界

- 不建立真实 HTTP/2 连接、不校验证书、不访问外部站点。
- 帧方向是诊断模型的一部分，不是 TLS/明文 HTTP/2 抓包实现。
- RST_STREAM 在途额度回收是题目要求的诊断账目规则；它不等同于 RFC 7540 对真实连接窗口的自动返还语义。
- 状态推进只使用事件注入的 `clock_ms`；文件锁等待和 SQLite 内部超时使用系统计时，但不进入连接状态或摘要。
