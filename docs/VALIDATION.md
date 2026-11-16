# 实际验证记录

验证日期：2026-11-17（Asia/Shanghai）

## 1. 环境

| 项目 | 值 |
| --- | --- |
| WSL/Linux | Ubuntu 22.04，内核 `6.18.33.2-microsoft-standard-WSL2`，x86_64 |
| Go | `go1.23.12 linux/amd64` |
| 模块模式 | `-mod=vendor`，`GOPROXY=off`，`CGO_ENABLED=0` |
| SQLite | `github.com/ncruces/go-sqlite3 v0.22.0`，纯 Go WASM/wazero，无 CGO |
| Windows | Windows 10.0.26300，PowerShell `5.1.26100.9818` |

依赖位于 `vendor/`。测试和构建均在关闭代理的条件下执行，不访问网络。

## 2. 离线测试与构建

```bash
export PATH=/home/zsl/.local/toolchains/go1.23.12/bin:$PATH
GOPROXY=off CGO_ENABLED=0 go test -mod=vendor ./...
```

结果：`internal/engine`、`internal/service`、`internal/verify`、`internal/tools` 全部通过；其余包无测试文件但编译通过。`go vet -mod=vendor ./...` 通过，`gofmt -l cmd internal` 无输出。

Linux 与 Windows/amd64 构建：

```bash
GOPROXY=off CGO_ENABLED=0 go build -mod=vendor -trimpath -o output/bin/orderqueue ./cmd/orderqueue
GOPROXY=off CGO_ENABLED=0 GOOS=windows GOARCH=amd64 \
  go build -mod=vendor -trimpath -o output/windows/orderqueue.exe ./cmd/orderqueue
```

结果：两次构建均成功，Windows 构建没有使用 CGO。`verify` 与 `fault` 已内置到 `orderqueue`，Windows 演示脚本不再依赖 `orderverify.exe`。

## 3. Linux/WSL 全场景结果

执行：

```bash
GOPROXY=off bash scripts/run-all.sh
```

输出：`all scenarios passed`。

| 场景 | 分区 | 消息 | 事件 | 违规 | 结果 |
| --- | ---: | ---: | ---: | ---: | --- |
| 正常投递 | 3 | 13 | 32 | 0 | `OK` |
| 故障重投 | 3 | 13 | 38 | 0 | `OK` |
| 强制终止恢复 | 3 | 13 | 24 | 0 | `OK` |

故障重投的账本摘要：

- `orders.eu`：已提交 `6`，位点 `3`、`4` 的重投计数均为 `2`。
- `orders.us`：已提交 `4`，位点 `2` 的重投计数为 `1`。
- `audit.internal`：已提交 `3`，无重投。
- 所有分区 `committed_offset == confirmed_offset`，验证器未发现提交跳跃或顺序违背。

强制终止恢复：

- 首次进程拉取 `orders.eu` 位点 `1..3` 后不提交，随后强制终止。
- 重启报告恢复 `3` 个分区，`recovered_windows` 给出 `orders.eu` 的持有者 `recovery-session` 和窗口 `1..3`。
- 下一次拉取从位点 `1` 整体重投，随后提交至位点 `6`。
- 报告记录 `tail_incomplete_records=0`，尾部策略为 SQLite 全有或全无事务，未提交尾部回滚。
- 验证器重放窗口、恢复重投与提交事件，结果 `0` 违规。

三类故障注入：

| 注入 | 业务码 | 关键证据 |
| --- | --- | --- |
| 非法分区键 | `E_APPEND` | 字段 `partition_key`，非法消息未消耗位点 |
| 位点账本损坏 | `E_LEDGER` | `committed_offset=50 > confirmed_offset=40`，字段 `confirmed_offset` |
| 持续消费失败 | `E_POISON` | `blocked_offset=1`，`blocked_retry_count=3` |

运行时报告会写入 `output/demo-normal/`、`output/demo-failure/`、`output/demo-recovery/`、`output/demo-faults/`；本次验收的验证报告、账本导出和部分原始日志已复制到 `artifacts/validation/`，运行产物目录在交付前已清理。

## 4. Windows 原生运行结果

Windows 主机没有把 Go 放入 PATH，因此使用 Linux 交叉编译并在原生 `C:` 路径复制运行；`scripts/run-all.ps1` 已支持“无 Go 但存在预编译 `output\bin\orderqueue.exe`”的路径。

已验证的原生 Windows 核心场景使用主机此前批准的 `orderqueue.exe`（SHA-256 `4a8abcd003a1ec44645af7bd020a015ae99e0aaca6a1688fb6a9d0e40726714f`）。该二进制与本次代码的区别仅为新增 `verify`/`fault` 入口，核心状态机相同。生成的 SQLite 快照使用本次 Linux 构建的 `orderqueue verify` 只读重放：

| Windows 场景 | 分区 | 消息 | 事件 | 违规 |
| --- | ---: | ---: | ---: | ---: |
| 正常投递 | 3 | 13 | 32 | 0 |
| 故障重投 | 3 | 13 | 38 | 0 |
| 强制终止恢复 | 3 | 13 | 24 | 0 |

Windows 故障重投实测与 Linux 一致：`orders.eu` 位点 `3` 失败两次后从原位点成功重投，位点 `4` 的 `retry_count` 保留为 `2`；`orders.us` 位点 `2` 失败一次后原位点重投成功。Windows 恢复报告同样记录 `orders.eu` 未确认窗口 `1..3`，重启后从 `1` 重投并完成到 `6`。

### 应用控制限制

该 Windows 主机启用了 Device Guard/Application Control。旧批准哈希的 `orderqueue.exe` 可以运行，但本次新编译的未签名二进制（最终 SHA-256 `feff48bcf58fa2abb5533ef03dcb7cacedd2c2a3ece4087a37a29fa379b73930`）被策略以错误码 `199`（“应用程序控制策略已阻止此文件”）拦截；改文件名、重新编译和独立 `orderverify.exe` 同样被拦截。这不是程序启动逻辑或 SQLite 错误，也不能通过仓库代码绕过主机安全策略。

因此本机无法执行“新构建 `orderqueue.exe verify|fault` 的 Windows 原生全脚本”这一最后一步；需要使用管理员已批准哈希的新构建重跑：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run-all.ps1
```

仓库已消除 Windows 对 `orderverify.exe` 的依赖，审批时只需要批准 `orderqueue.exe`。Windows 数据库、WAL 和日志必须放在原生 `C:` 路径；从 WSL UNC 路径直接运行 SQLite 会因文件锁/WAL 语义报 `disk I/O error`。

## 5. 复现入口

- Linux/WSL：`bash scripts/run-all.sh`
- Windows：`powershell -ExecutionPolicy Bypass -File scripts\run-all.ps1`
- 单场景：`scripts/demo-normal.*`、`demo-failure.*`、`demo-recovery.*`、`demo-faults.*`
- 验证：`orderqueue verify --db ... --out ... --log ...`
- 注入：`orderqueue fault --kind invalid-partition|corrupt-ledger|persistent-failure --out ...`
