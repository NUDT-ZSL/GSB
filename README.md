# 本地消息互操作投递中间件

面向 Windows 本地开发和演示的单进程消息中间件，聚焦 v1/v2/v3 三种消息格式的互操作、三种投递语义，以及强制终止后的文件恢复。通信只使用回环 TCP 上的 JSON 行协议，不依赖 Docker、数据库、云账号或第三方 Go 包。

## 已交付能力

- Go 标准库实现的 TCP 中间件：严格校验 v1/v2/v3 字段、类型和语义键，未知版本返回 `E_VERSION`，错误不会中断同连接后续请求。
- 跨版本转换：高版本向低版本降级时剥离字段；低版本向高版本补齐默认字段，并在转换记录中区分 `source`、`default`、`derived`、`mutated` 和 `declared`。
- 三种投递语义：`at_most_once`、`at_least_once`、`exactly_once`；转换后语义元数据仍然完整。
- 追加日志、消费者偏移快照、去重索引、CRC32 帧校验、尾部断帧修复、强杀恢复报告。
- 单写锁保证同一分区内的日志顺序与投递顺序一致；恰好一次发布在并发下只产生一个可消费结果。
- Python 确定性生产者/消费者、三场景演示编排、结果验证器和 Tkinter 观察界面。
- Go 单元/集成测试与 Python 验证器测试。

## 环境要求

推荐 Windows 10/11 + Go 1.21 以上 + 官方 Python 3.10 以上。官方 Windows Python 安装包已包含 Tkinter。

```powershell
winget install GoLang.Go
winget install Python.Python.3.12
go version
python --version
python -c "import tkinter; print(tkinter.TkVersion)"
```

Linux 如缺少 Tkinter：

```sh
sudo apt-get install python3-tk
```

## 快速开始

Windows PowerShell：

```powershell
.\scripts\build.ps1
python -m unittest discover -s tests -v
.\scripts\demo.ps1
python tools\gui.py --data-dir .runtime\demo-data
```

Linux/macOS：

```sh
./scripts/build.sh
go test ./...
python3 -m unittest discover -s tests -v
./scripts/demo.sh
python3 tools/gui.py --data-dir .runtime/demo-data
```

演示脚本会清空 `.runtime/demo-data`，依次执行混合版本、订阅者重启、强制终止恢复三个场景，并把结果写入 `artifacts/demo/`。

单独启动中间件：

```powershell
go run .\cmd\middleware -addr 127.0.0.1:19090 -data .runtime\data -retry-after 1s
```

或使用 `.\scripts\run.ps1` / `./scripts/run.sh`。监听地址会被限制为回环地址。

## 实测结果

最近一次完整执行结果：

| 检查 | 结果 |
|---|---|
| `go test ./...` | 通过：protocol、store、engine 全部测试 |
| `python3 -m unittest discover -s tests -v` | 通过：3 个验证器正向/负向测试 |
| `python3 tools/demo.py` | 进程退出码 0，`E_VERIFY` 校验结果 `OK` |
| 混合投递 | 12 条持久化消息，22 次实际投递，19 次确认 |
| 转换覆盖 | 10 次降级、5 次升版补齐、7 次同版本 |
| 字段追踪 | 28 个被剥离字段实例、14 个补齐字段实例 |
| 恰好一次 | 3 个唯一幂等键；12 路并发同键发布收敛为 1 次接受、11 次重复 |
| 重投 | v3 未确认消息重启后 attempt 2、语义重试计数 1、消费者偏移保持不变 |
| 强杀恢复 | 截断 49 字节尾部断帧，重建 3 个去重索引，已确认消息未重投 |
| 跨平台 | `GOOS=windows GOARCH=amd64 go build` 成功 |
| GUI 冒烟 | 实际渲染 22 条投递、12 条消息、7 个消费者，窗口 1280×760 |

完整机读结果见 `artifacts/demo/scenario-report.json`、`artifacts/demo/verification.json` 和 `artifacts/demo/recovery-report.json`。

## 架构

```text
生产者 JSONL ─┐
             ├─ 回环 TCP ─ 协议校验 ─ 串行投递引擎 ─ 追加日志/快照
消费者 JSONL ─┘                          │
                                         ├─ 版本转换管线
                                         ├─ 三种语义判定
                                         └─ 去重与恢复
                                                   │
                                 Python 验证器 / Tkinter 界面
```

主要目录：

- `cmd/middleware/`：TCP 服务、连接状态机、发布/订阅/确认处理。
- `internal/protocol/`：字段集合、严格校验、转换矩阵和协议类型。
- `internal/engine/`：消费者状态、语义、去重、偏移量和恢复重建。
- `internal/store/`：CRC32 帧日志、尾部修复、原子 JSON 快照。
- `clients/`：可复用 Python 客户端和样本生产者/消费者。
- `tools/verify.py`：逐条验证转换与语义自洽性。
- `tools/gui.py`：Tkinter 投递观察台。
- `tools/demo.py`：三个场景的确定性编排。

## 协议摘要

发布请求：

```json
{"type":"publish","semantics":"exactly_once","message":{"version":3,"delivery_id":"m3","body":{"text":"hello"},"timestamp_ms":1700000000000,"retry_count":0,"partition_key":"orders","idempotency_seq":7002}}
```

成功响应包含 `accepted`、`duplicate`、`global_offset`、`source_version` 和语义信息。重复幂等键返回既有结果：

```json
{"ok":true,"type":"publish_ack","accepted":false,"duplicate":true,"delivery_id":"m3","global_offset":5,"existing_result":{"delivery_id":"m3","global_offset":5,"accepted_at":"..."}}
```

订阅和确认：

```json
{"type":"subscribe","consumer_id":"viewer-v1","version":1,"partition_key":"orders"}
{"type":"ack","consumer_id":"viewer-v1","delivery_id":"m3","offset":1}
```

服务端先返回 `subscribe_ack`，随后推送 `delivery`。每条投递包含 `payload`、`conversion`、`semantic`、`consumer_offset` 和 `global_offset`。错误统一为：

```json
{"ok":false,"type":"error","error":{"code":"E_VERSION","message":"unsupported message version 9"}}
```

严格字段集合与完整消息示例见 `docs/protocol.md`。

## 版本互操作规则

| 源版本 | v1 订阅者 | v2 订阅者 | v3 订阅者 |
|---|---|---|---|
| v1 | 原样 | 补齐 `timestamp_ms=0`、`retry_count` | 补齐四个 v3/v2 字段 |
| v2 | 剥离 `timestamp_ms`、`retry_count` | 原样或更新重试计数 | 补齐 `partition_key=_default`、`idempotency_seq=0` |
| v3 | 剥离时间戳、重试、分区键、幂等序列 | 剥离分区键、幂等序列 | 原样或更新重试计数 |

确定性默认值：

- `timestamp_ms = 0`
- `retry_count = 0`
- `partition_key = "_default"`
- `idempotency_seq = 0`

`filled_fields` 只记录订阅版本支持、源版本不存在的字段。`field_origins` 标记字段来源；低版本源在重投时产生的非零重试计数标为 `derived`，源字段被重试逻辑改写时标为 `mutated`。`source_delivery_id`、`source_version` 和 `conversion_id` 保证转换可回溯。

## 投递语义

- `at_most_once`：首次调度即视为完成，不因未确认或重启而重投；确认请求可接受但非必需。
- `at_least_once`：未确认消息在重连后立即重投；连接存续期间按 `-retry-after` 周期重投。重试计数为 `源 retry_count + attempt - 1`，消费者偏移保持不变。
- `exactly_once`：必须是 v3 且 `idempotency_seq > 0`，否则 `E_SEMANTIC`。发布阶段使用全局幂等序列去重；同一序列的并发请求只写入一次消息日志和一次去重索引，所有重复请求稳定返回既有结果。未确认结果可按原幂等键重投，确认后不重投。

消费者语义来自消息本身。订阅声明中的可选 `semantics` 只作为提示记录，不改变消息的语义。

## 持久化与恢复

数据目录包含：

| 文件 | 用途 |
|---|---|
| `messages.log` | 已接收消息，含投递标识、格式版本、主体、分区、语义和全局偏移 |
| `deliveries.log` | 每次转换与投递尝试，含消费者偏移、attempt、转换字段记录和语义判定 |
| `acks.log` | 消费者确认，含消费者偏移和全局偏移对应关系 |
| `dedup.index` | 恰好一次幂等索引，可依据消息日志重建 |
| `consumer-offsets.json` | 原子替换的消费者偏移快照 |
| `recovery-report.json` | 恢复偏移、重建去重条目数、尾部处理与消费者状态 |

每个日志行是独立帧：

```json
{"seq":1,"crc32":"1a2b3c4d","record":{"...":"..."}}
```

恢复时逐行校验序号和 CRC32。末尾没有换行、JSON 不完整、序号断裂或 CRC 不匹配的尾部帧会被截断，并在恢复报告中记录字节偏移、字节数和 `truncated_incomplete_tail`；中间帧损坏会阻止启动，避免静默跳过状态。偏移和去重索引均由日志重建，因此强杀不依赖操作系统缓冲成功落盘。

## 验证器

```powershell
python tools\verify.py --data-dir .runtime\demo-data --output artifacts\demo\verification.json
```

验证器逐条检查：

- 源消息版本、标识、偏移和语义；
- 声明版本、转换方向、源/目标字段集合；
- 被剥离字段和补齐字段是否与订阅版本一致；
- 默认值、时间戳、分区键、幂等键和重试计数；
- `field_origins` 是否区分原始值和补齐值；
- 消费者偏移是否单调、确认偏移是否对应投递；
- 确认后是否再次投递；
- 是否出现重复幂等消息、重复去重索引或确认后重投。

失败返回 `E_VERIFY`，`errors[]` 中每条包含偏差的 `delivery_id`、`field`、`expected`、`actual` 和说明。负向测试会故意篡改剥离字段和确认偏移，确认工具能报告而非放行。

## Tkinter 界面

```powershell
python tools\gui.py --data-dir .runtime\demo-data
```

界面包含四个稳定排序的页签：

- 投递流：日志序号、投递标识、消费者、源版本、声明版本、方向、剥离/补齐字段、偏移、语义和判定。
- 已接收消息：源版本、语义、分区键和消息体。
- 消费者偏移：下一个偏移、已确认偏移、未完成和已完成数量。
- 恢复与验证：恢复报告、消费者快照、日志错误和去重条目数。

无图形会话时可做数据检查：

```powershell
python tools\gui.py --data-dir .runtime\demo-data --check
python tools\gui.py --data-dir .runtime\demo-data --smoke-test
```

## 独立客户端

```powershell
python clients\producer.py --port 19090 --input samples\mixed.jsonl
python clients\consumer.py --port 19090 --consumer-id c1 --version 1 --count 5
```

`producer.py --workers N` 可执行并发发布；`consumer.py --no-ack` 可复现未确认状态；`--assert-no-delivery` 可检查确认后或至多一次消息不再重投。

## 测试

```sh
go test ./...
python3 -m unittest discover -s tests -v
python3 tools/demo.py
```

Go 测试覆盖字段/类型/版本拒绝、九种转换组合、默认值和字段来源、串行与并发恰好一次去重、至多一次不重投、至少一次重启重投、分区顺序、并行分区顺序、多消费者并发顺序、日志尾帧修复和并发日志帧完整性。Python 测试覆盖验证器通过路径、字段偏差 `E_VERIFY` 和确认偏移偏差。

## 边界与假设

- 本系统定位本机演示和语义验证，不提供 TLS、鉴权、多节点复制或对外网络监听；启动时拒绝非回环地址。
- 单个消费者标识同时只允许一个活动订阅；重复幂等键以 `idempotency_seq` 全局收敛。
- 新消费者从匹配其分区过滤条件的现存最早消息开始消费。消费者偏移按“唯一消息首次投递”分配，重投复用原偏移。
- 此实现不实现生产级背压和无限历史归档；日志会随演示持续追加，清空数据目录即可重置。

更详细的字段与错误码见 `docs/protocol.md`，并发、持久化和恢复不变量见 `docs/design.md`。
