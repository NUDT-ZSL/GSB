# localmesh / meshctl

面向本地 Windows 环境的服务网格流量治理工具。实现语言为 Go，仅使用标准库，不依赖 Docker、Python 包或任何外部服务。所有上游均为 `127.0.0.1` 高位端口上的本地模拟 HTTP 服务。

## 能力概览

- 服务、实例、路由规则加载与强校验；失败时整体拒绝并返回 `E_RULE`，绝不部分加载。
- 全局并发预算 + 每服务并发配额双层约束；等待中的请求不会绕过任何一层配额。
- 请求级单次尝试超时和重试预算；每次尝试使用单调时钟计时并写入轨迹。
- 确定性加权实例选择；实例连续失败达到阈值后进入熔断，冷却后半开试探。
- 调度轨迹、结果快照、验证报告、路由版本差异全部落盘。
- 规则版本由文件 SHA-256 与声明版本共同识别；当前快照整体替换，旧版本快照保留。
- checkpoint 持久化在途预算、配额占用、熔断状态和已完成轨迹；`recover` 将未完成请求标为 unknown 并生成重跑请求文件。
- `E_RULE`、`E_VERIFY`、`E_UPSTREAM`、`E_SNAPSHOT`、`E_REPLAY` 稳定业务码和 JSONL 结构化日志。

## 目录

```text
cmd/meshctl/              CLI 入口（run/verify/diff/recover/mock）
internal/rules/           规则解析与强校验
internal/scheduler/       双预算调度、HTTP 试调、checkpoint
internal/circuit/         闭/开/半开熔断状态机
internal/selection/       确定性权重选择
internal/snapshot/        原子快照/轨迹/报告存储
internal/verify/          轨迹、结果、配额、熔断一致性验证
internal/recovery/        强制终止恢复与 E_REPLAY 重放校验
internal/mockupstream/    本地多端口模拟上游
configs/                  v1/v2 规则、正常/故障/异常/崩溃请求样本
scripts/demo.sh           Linux/WSL 演示
scripts/demo.ps1          Windows PowerShell 演示
```

## 环境与构建

需要 Go 1.21 或更高版本，并确保 `go` 在 `PATH` 中。

Windows PowerShell：

```powershell
go version
go build -o bin\meshctl.exe .\cmd\meshctl
.\bin\meshctl.exe version
```

WSL / Linux：

```bash
go version
go build -o bin/meshctl ./cmd/meshctl
./bin/meshctl version
```

Windows 交叉编译已通过：

```text
GOOS=windows GOARCH=amd64 go build -o meshctl.exe ./cmd/meshctl
PE32+ executable (console) x86-64, for MS Windows
```

## 规则文件

```json
{
  "schema_version": 1,
  "version": "v1",
  "global_concurrency": 4,
  "services": [
    {
      "name": "catalog",
      "quota": 2,
      "instances": [
        {
          "id": "catalog-a",
          "address": "127.0.0.1:18101",
          "weight": 3,
          "failure_threshold": 2,
          "cooldown_ms": 500
        }
      ]
    }
  ],
  "routes": [
    {
      "prefix": "/api/catalog",
      "service": "catalog",
      "timeout_ms": 800,
      "retries": 1
    }
  ]
}
```

约束：

- 服务名和实例 ID 必须唯一且非空。
- 实例地址必须是回环 IP 字面量加端口，不接受 `localhost` 等主机名；端口范围 1..65535。
- 权重为非负整数；同一服务的权重和不得为零；熔断阈值和冷却毫秒数必须为正整数。
- 路由前缀必须以 `/` 开头；目标服务必须存在；`timeout_ms` 为正整数；`retries` 为非负整数。
- `retries` 表示首次尝试之后的重试次数，因此尝试预算为 `retries + 1`。
- 任一字段不合法都会返回 `E_RULE` 和 `field` 定位，同时日志中包含全部 `violations`。

请求文件：

```json
{
  "round": "cold",
  "requests": [
    {"id": "cold-001", "method": "GET", "path": "/api/catalog/items?delay_ms=15"},
    {"id": "cold-002", "method": "POST", "path": "/api/orders", "body": "{\"item\":1}"}
  ]
}
```

## CLI

```text
meshctl run     -rules FILE -requests FILE -round NAME [-out DIR] [-log FILE]
                [-inject-failure none|rule|upstream|snapshot]
meshctl verify  -snapshot FILE [-trace FILE] [-report FILE] [-log FILE]
meshctl diff    -from SNAPSHOT -to SNAPSHOT [-out FILE]
meshctl recover [-checkpoint FILE] -out DIR [-log FILE]
meshctl mock    -endpoint ADDRESS=MODE:DELAY_MS[:FAIL_COUNT] ... [-ready-file FILE]
```

`mock` 的 mode 为 `ok`、`fail` 或 `flaky`；`flaky` 的前 N 次返回 HTTP 500，之后恢复 200。

### 业务码与退出状态

| Code | Exit | 含义 |
|---|---:|---|
| `E_RULE` | 2 | 规则/请求损坏、校验失败、轮次中规则变更 |
| `E_VERIFY` | 3 | 快照与轨迹不自洽、配额超限、冷却期误选 |
| `E_UPSTREAM` | 4 | 上游不可达；结果快照仍会生成 |
| `E_SNAPSHOT` | 5 | 快照/轨迹/检查点写入失败或参数注入失败 |
| `E_REPLAY` | 6 | 恢复时最近完成轮次重放校验失败 |

所有业务错误都会写入 JSONL 日志，字段包括 `ts`、`level`、`code`、`event`、`round`、`field`、`message`。

## 调度与验证语义

- 加权选择：以 `round_id + request_id + attempt_no` 的稳定哈希在加权环上定位；候选顺序由权重区间决定，同输入的候选序列可复现。
- 熔断：连续失败计数达到 `failure_threshold` 后打开；冷却期内不派发网络尝试；冷却后允许一个半开探测，成功关闭，失败重新打开。
- 重试：单次尝试的结果只记录为 `success` 或 `failure`；后续尝试不会改写之前的尝试语义。
- 并发：`request_start` 到 `request_end` 期间同时持有全局和服务配额。验证器从轨迹时间区间重算峰值，且同时校验尝试级并发。
- 轨迹计时：`start_mono_ns`/`end_mono_ns` 使用单调时钟；`duration_ms` 为毫秒观测值。
- 验证偏差：按请求 ID、字段、消息稳定排序，报告首个偏差请求与字段。

## 快照、版本与恢复

输出目录主要文件：

```text
snapshot-<round>-<version>.json   完整结果快照（历史保留）
current.json                      最近一次完整快照（原子整体替换）
trace-<round>-<version>.jsonl     调度轨迹
checkpoint-<round>.json           轮次检查点
checkpoint-current.json           最近检查点
verify-<round>.json               验证报告
route-diff.json                   路由与实例差异
rerun-<round>.json                恢复后未完成请求的重跑清单
mesh.log                          JSONL 结构化日志
```

规则版本为 `<declared_version>@sha256:<file hash prefix>`。每轮只使用启动时加载的不可变规则集；轮次结束会重新计算文件哈希，如果轮次期间被修改则返回 `E_RULE`，不把混合结果安装为 `current.json`。

`recover` 先重新验证最近完成快照；不一致返回 `E_REPLAY`。否则保留已完成请求，把所有 pending/running 请求标记为 `unknown`、`needs_rerun=true`，并在恢复快照中保留熔断状态与崩溃时的配额占用。恢复快照明确设置 `exit_report_available=false`，不会伪称生成退出报告。

## 本地演示

演示使用端口 `18101` 到 `18106`，另用未监听的 `18199` 模拟不可达上游。运行前需保证端口空闲。

WSL / Linux：

```bash
bash scripts/demo.sh out/demo
```

Windows PowerShell：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\demo.ps1
```

演示顺序固定为：

1. 冷启动轮次（v1）
2. 规则替换后的热轮次（v2）
3. 版本/路由差异
4. 故障实例、不可达上游、熔断轮次
5. `E_UPSTREAM` 命令行注入
6. 损坏规则文件与 `E_RULE` 命令行注入
7. `E_SNAPSHOT` 命令行注入
8. 异常后的正常轮次，证明进程状态未被污染
9. `kill -9` / `Stop-Process -Force` 后恢复、重放校验与 unknown 重跑

每个正常/故障结果轮次都会调用独立 `meshctl verify`，并输出：轮次号、规则版本、请求总数、成功/失败/unknown 数、熔断触发次数、全局与每服务配额峰值。

## 测试

```bash
go test ./...
go test -race ./...
```

测试覆盖规则强校验、确定性权重、熔断半开和快速失败、重试预算、双配额并发、快照版本整体替换、验证偏差定位、checkpoint unknown 恢复和 `E_REPLAY`。

本次环境已执行 `go test ./...` 与 `go vet ./...`。由于当前 WSL 未安装 gcc，`go test -race ./...` 未执行；安装 C 编译器后可直接运行。

## 实际验证结果

当前环境为 Go 1.23.6（WSL Ubuntu 22.04），Windows amd64 交叉编译成功。完整演示的关键结果见 `docs/run-results.txt`。样例结果：

```text
VERIFY round=cold  rules_version=v1@sha256:832c90fb05b416bf total=16 success=16 failure=0 unknown=0 breaker_triggers=0 quota_peak_global=4 quota_peak_service={catalog=3,orders=1}
VERIFY round=hot   rules_version=v2@sha256:a6890e4efd975be5 total=12 success=12 failure=0 unknown=0 breaker_triggers=0 quota_peak_global=4 quota_peak_service={catalog=3,orders=1}
VERIFY round=fault rules_version=v2@sha256:a6890e4efd975be5 total=12 success=10 failure=2 unknown=0 breaker_triggers=2 quota_peak_global=3 quota_peak_service={dead=1,faulty=2}
CRASH exit=137 checkpoint=.../checkpoint-current.json
RECOVER round=crash status=recovered unknown=20 rerun_file=... replay_checked=post-failure exit_report=unavailable
VERIFY round=crash-retry ... total=20 success=20 failure=0 unknown=0
```

异常链路实际输出：

```text
E_RULE: must start with / (validate_rules, field=routes[0].prefix)
E_RULE: rules file corruption injected by command line (load_rules, field=inject_failure)
E_UPSTREAM: one or more upstream attempts were unreachable (dispatch)
E_SNAPSHOT: snapshot write failure injected by command line (write_snapshot, field=inject_failure)
```

`route-diff.json` 可辨识地列出：`/api/catalog` 修改、`/api/orders/priority` 新增、`/api/slow` 新增，以及 `catalog-b` 移除、`catalog-c` 新增、`catalog-a` 权重修改。
