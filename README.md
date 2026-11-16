# orderqueue：本地顺序消息中间件

面向本地 Windows/WSL 环境的单进程顺序消息中间件。服务端在回环 TCP 上接收 JSON 行协议，按分区键分配连续追加位点，以唯一投递窗口提供有序拉取，并把消息日志、消费位点账本、窗口与重投计数持久化到本地 SQLite。

## 核心保证

- 追加位点在分区内从 1 开始严格连续递增；校验失败返回 `E_APPEND`，且不消耗位点。
- 消息、分区尾位点和追加事件在同一 SQLite 事务中提交；事务提交后才返回追加回执。
- 每个分区最多一个活跃投递窗口，其他会话并发拉取返回 `E_FENCE` 和持有者会话标识。
- 失败、超时、进程重启后的未确认窗口都从最早未确认位点整体重投，不允许后序消息越过前序消息被确认。
- 提交只接受当前窗口内的连续前缀确认；越窗、回退或跳跃提交返回 `E_COMMIT`，原账本不变。
- 每个位点独立记录重投次数；超过 `--max-retries` 时返回 `E_POISON`，输出阻塞位点和计数，分区停止投递。
- 分区级互斥串行化拉取、提交、失败和超时检查；提交与超时竞争只有一个事件胜出。
- 窗口超时使用包含单调时钟分量的服务端时间，SQLite 中同时保存墙钟期限用于恢复/审计。
- 格式版本或分区账本版本变化时，按追加日志与事件日志整体重建分区账本，不做新旧字段增量合并。
- 独立验证器按分区重放追加、提交、失败、超时、毒点与解阻事件，发现不一致返回 `E_VERIFY`；`orderqueue verify` 与 `orderqueue fault` 已内置同一实现。

## 环境与离线构建

要求 Go 1.21 或更高版本。项目使用纯 Go 的 `ncruces/go-sqlite3` 驱动（SQLite WASM + wazero），不需要 CGO、SQLite 动态库、容器或数据库服务端。

所有依赖已放入 `vendor/`，构建命令固定使用 `-mod=vendor`：

```bash
export CGO_ENABLED=0
go build -mod=vendor -trimpath -o output/bin/orderqueue ./cmd/orderqueue
# 可选：保留独立入口，内部与 orderqueue verify|fault 共用实现
go build -mod=vendor -trimpath -o output/bin/orderverify ./cmd/orderverify
```

Windows 构建输出 `output\bin\orderqueue.exe`；默认演示脚本只使用该文件，不再依赖 `orderverify.exe`。项目已通过 `GOOS=windows GOARCH=amd64 CGO_ENABLED=0` 交叉编译验证。若 Windows 缺少 Go，但已放置预编译的 `output\bin\orderqueue.exe`，`run-all.ps1` 会直接使用该文件。

Windows 必须把数据库、日志和二进制放在原生 `C:`（或其他本地 NTFS）路径下运行，不要直接从 `\\wsl$`/WSL UNC 工作目录打开 SQLite。企业 Device Guard/Application Control 若只信任已批准哈希，新构建的二进制需要先由管理员审批；本仓库的 Windows 演示不依赖 `orderverify.exe`，可避免额外的独立可执行文件审批。

## 快速验收

WSL/Linux：

```bash
bash scripts/run-all.sh
```

Windows PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run-all.ps1
```

脚本顺序执行：

1. `demo-normal`：三个分区并行正常消费。
2. `demo-failure`：注入瞬时失败并按原顺序重投。
3. `demo-recovery`：拉取窗口后强制终止服务，重启并完成有序重投。
4. `demo-faults`：分别复现 `E_APPEND`、`E_LEDGER`、`E_POISON`。

运行前会检查 `19160`、`19161`、`19162` 是否被占用。运行产物写入 `output/<scenario>/`，包括 SQLite 数据库、恢复报告、结构化日志、消费者标记、账本导出和验证报告。

## 手工运行

启动服务：

```bash
./output/bin/orderqueue serve \
  --listen 127.0.0.1:19160 \
  --db output/orderqueue.db \
  --log output/server.jsonl \
  --recovery-report output/recovery-report.json \
  --ready-file output/server-ready.json \
  --max-retries 3 \
  --window-timeout 5s
```

追加确定性样本：

```bash
./output/bin/orderqueue produce \
  --addr 127.0.0.1:19160 \
  --input data/producer.jsonl \
  --report output/producer-report.json
```

消费一个分区：

```bash
./output/bin/orderqueue consume \
  --addr 127.0.0.1:19160 \
  --partition-key orders.eu \
  --session demo-consumer \
  --limit 3 \
  --window-timeout-ms 5000 \
  --failure-plan data/failure-injection.json \
  --marker-out output/consumer-markers.jsonl \
  --report output/consumer-report.json \
  --target-offset 6
```

查询与验证：

```bash
./output/bin/orderqueue status --addr 127.0.0.1:19160 --out output/status.json
./output/bin/orderqueue dump --db output/orderqueue.db --out output/ledger-dump.json
./output/bin/orderqueue verify --db output/orderqueue.db --out output/verification-report.json --log output/verify.jsonl
```

三类故障注入：

```bash
./output/bin/orderqueue fault --kind invalid-partition --out output/fault-invalid.json
./output/bin/orderqueue fault --kind corrupt-ledger --out output/fault-ledger.json
./output/bin/orderqueue fault --kind persistent-failure --out output/fault-poison.json
```

`cmd/orderverify` 仍提供等价的 `check`/`fault` 入口，仅作为独立工具或兼容入口保留。

## 确定性样本

- `data/producer.jsonl`：13 条消息，覆盖 `orders.eu`、`orders.us`、`audit.internal` 三个分区。
- `data/failure-injection.json`：`orders.eu` 位点 3 失败两次，`orders.us` 位点 2 失败一次。
- `data/failure-poison.json`：持续失败参数，用于超过重投上限的注入。
- `data/no-failures.json`：正常场景空故障计划。
- `data/mark-result.sh` / `data/mark-result.ps1`：消费者结果标记脚本。
- `data/run-producer.*` / `data/run-consumer.*`：跨平台样本生产者与消费者包装脚本。

## 目录

```text
cmd/orderqueue       服务、生产者、消费者、status/dump/verify/fault 命令
cmd/orderverify      可选的独立验证器与故障注入兼容入口
internal/tools       verify/fault 命令行实现，两个入口共用
internal/engine      分区状态机、窗口、重投、超时、恢复
internal/store       SQLite 表结构、事务写入、版本重建
internal/service     JSONL TCP 服务
internal/verify      独立事件重放验证
data                 确定性样本、失败计划、标记脚本
scripts              WSL/Linux 与 Windows PowerShell 演示脚本
docs                 协议、设计、验证记录
artifacts/validation 实际验收报告快照
output               运行时数据库、日志、报告与二进制
```

## 测试

```bash
go test -mod=vendor ./...
```

测试覆盖位点原子性、生产者序列校验、窗口围栏、提交收紧、顺序重投、超时/提交唯一胜者、毒点阻塞、人工解阻、强制重建恢复、版本整替换、账本损坏、跨分区并发、TCP 协议和独立验证器。

详细设计见 `docs/DESIGN.md`，协议见 `docs/PROTOCOL.md`，实际验收结果见 `docs/VALIDATION.md`。
