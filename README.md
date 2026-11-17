# LB Health：本地负载均衡健康检查与恢复一致性组件

这是一个面向 Windows 10+ 的纯标准库实现。组件只访问 `127.0.0.0/8` 或 `::1` 回环地址，不需要 Docker、真实远端网络或外部账号。核心包括强校验配置、真实 TCP/HTTP 探测、四态健康机、慢启动权重、SQLite 轮次账本、强杀恢复、全量快照替换和一致性重放。

## 快速开始

要求 Python 3.11，并确认 `sqlite3`、`socket`、`subprocess`、`tkinter` 可用。

```powershell
cd <项目目录>
py -3.11 -m unittest discover -v
py -3.11 demo.py --port-start 19461 --port-end 19520
```

演示会依次执行：

1. 冷启动收敛（1–5 轮）。
2. 本地注入间歇失败后的迁移与恢复（6–10 轮）。
3. 未完成轮次强杀、恢复重跑、快照和正/负一致性重放（11–14 轮）。

每个场景顶层都包含 `rounds_run`、扁平化的 `transitions`、逐轮探测/可用集/权重/校验记录，以及最终 `conclusion`。

输出文件：

- `artifacts/demo_report.json`：稳定排序的完整演示报告。
- `artifacts/run/ledger.sqlite`：包含 complete、unknown、重跑 attempt 的 SQLite 账本。
- `artifacts/run/config.json`：本次实际端口渲染后的配置。
- `artifacts/run/behaviors/*.json`：模拟后端行为脚本。

打开界面：

```powershell
py -3.11 -m lbhealth ui --config artifacts/run/config.json --db artifacts/run/ledger.sqlite
```

界面启动冒烟：

```powershell
py -3.11 -m lbhealth ui --smoke --config config/sample_pool.json --db artifacts/ui_ledger.sqlite
```

若端口被占用，演示会从指定区间自动选取 4 个空闲高位端口；可用 `--port-start`/`--port-end` 改变区间。

## 配置格式

样本见 `config/sample_pool.json`。所有成员端口、协议和行为均可本地配置；`probe_protocol` 是池默认协议，成员可用 `protocol` 覆盖，从而在同一池中混合 HTTP 与 TCP。

```json
{
  "schema_version": 1,
  "config_version": "demo-v1",
  "pool": {
    "probe_protocol": "http",
    "interval_ms": 320,
    "timeout_ms": 90,
    "success_threshold": 2,
    "failure_threshold": 2,
    "slow_start_weight_table": [25, 50, 75, 100],
    "members": [
      {
        "id": "alpha",
        "address": "127.0.0.1",
        "port": 19461,
        "protocol": "http",
        "weight": 50,
        "probe_path": "/health"
      }
    ]
  }
}
```

校验规则和失败行为：

- 地址必须是 IP 字面量且为回环地址；拒绝 `localhost`、私网地址和通配地址。
- 端口必须为 `1..65535`。
- 成员标识唯一；HTTP 路径必须以 `/` 开头且不能带空白或换行。
- `interval_ms`、`timeout_ms`、成功/失败阈值必须为正整数，且 `timeout_ms < interval_ms`。
- 成员权重必须为非负整数，同池权重和不得为零。
- 慢启动表必须是严格递增的整数百分比，最终值为 100，从而保证恢复后达到满额权重。
- 任一错误会整体拒绝加载，返回 `E_POOL` 与精确字段路径，例如 `$.pool.members[2].id`。

```powershell
py -3.11 -m lbhealth validate --config config/sample_pool.json
```

## 状态机与权重

状态为 `unknown`、`healthy`、`unhealthy`、`draining`。每轮对每个成员执行一次真实探测，轮末统一结算：

- `unknown` 达到连续成功阈值 → `healthy`；达到连续失败阈值 → `unhealthy`。
- `healthy` 达到连续失败阈值 → `unhealthy`。
- `unhealthy` 达到连续成功阈值 → `healthy`，慢启动从表首重新开始。
- `healthy` 的成功轮次推进慢启动索引；未达失败阈值的失败也会中断并重置慢启动，但不改变 `healthy` 状态。
- `draining` 不参与自动恢复；管理员恢复后进入 `unknown` 并重新积累阈值。
- Drain 请求在当前在途轮次结束后、下一轮开始时生效；该轮仍完成探测，但轮末可用集合和权重立即排除该成员。

每轮结束状态决定权重，不混用轮内旧值：

```text
healthy 成员: floor(成员满额权重 * 慢启动百分比 / 100)
其他状态:      0
```

账本和报告同时保存每成员 `base_weight`、`slow_start_index`、`slow_start_percent`、`effective_weight`、轮次及满额权重和，`validate_round` 会逐项重算并检查公式、可用集合和总权重。

## 真实探测与后端模拟

`scripts/backend_sim.py` 以独立子进程监听回环端口，支持：

- HTTP：返回 `200`/`503`，可设置服务端真实延迟。
- TCP：响应 `PONG`/`FAIL`。
- 行为计划由本地 JSON 控制；`scripts/inject_behavior.py` 原子替换行为文件。
- “连接拒绝”通过故意不启动对应监听进程产生，而不是伪造探测异常。

探测执行器真正使用 `socket.create_connection`、`sendall`、`recv` 和套接字超时。慢响应超时来自实际 `socket.timeout`，不存在用 `sleep` 直接制造结论的路径。每次探测保存协议、起止单调时钟毫秒、响应摘要和单次结论。

## 账本、强杀与恢复

`Ledger` 使用 SQLite WAL + `synchronous=FULL`。每轮顺序如下：

1. 事务提交 `round_attempts(status=incomplete)`。
2. 获取每个成员真实探测结论。
3. 在单个事务中写入全部 probes/transitions，并原子更新为 `status=complete`。

进程在步骤 3 前或步骤 3 中强杀，只可能留下 `incomplete`。重新打开账本时：

- `incomplete` 被标记为 `unknown`，要求重跑。
- 恢复只读取最新 `complete` 轮的 `state_json`。
- 未解决 `unknown` 轮次会列入恢复报告；同轮后续 complete attempt 完成后不再要求重跑。
- 报告明确 `in_flight_results_recovered=false`，不会声称恢复在途探测或生成退出报告。
- 已 complete 轮次不可重写；重跑使用新的 attempt 序号。

演示中 `scripts/crash_advance.py` 在 incomplete 行提交后写入屏障并暂停，父进程使用 `Popen.kill()`（Windows 上为强制 TerminateProcess 语义）终止它。屏障不包含任何伪造或提前写入的探测结果。

## 快照与全量替换

快照保存在同一 SQLite 的 `snapshots` 表中，包含配置版本、成员摘要、完整运行状态、连续计数、慢启动进度、可用集合、生效权重、最近轮次摘要、重放基线及可选重放上下文。

恢复快照时始终使用 `mode=full_replace`：

- 不把快照增量合并到当前内存状态。
- 同 ID 成员状态和计数由快照整体覆盖。
- 当前配置新增但快照缺失的成员重置为 `unknown`、计数为 0、权重为 0。
- 快照存在但当前配置缺失的成员从恢复状态移除。
- 报告列出所有 `replaced_members`、`added_members`、`removed_members`、`reset_counters`，并分别给出配置版本、成员摘要和配置摘要是否匹配。

## 一致性重放

重放从快照记录的窗口前基线状态开始，按相同配置版本、成员集合和本地后端行为重新执行真实探测，逐轮比较：

1. 探测结论与失败码。
2. 状态迁移。
3. 可用成员集合。
4. 每成员生效权重。
5. 成员状态、连续计数、慢启动进度。

首个差异报告 `E_REPLAY`，包括轮次、成员、字段、期望值、实际值和具体依据。若配置版本、配置摘要、成员摘要或行为清单不一致，会在开始探测前直接说明不可复现依据。

`demo.py` 会先制造一次可控行为偏差，验证报告准确定位到第 11 轮 `bravo/probe.ok`；再恢复正确行为，验证第 11–14 轮全部一致。

## CLI

```powershell
# 校验配置
py -3.11 -m lbhealth validate --config config/sample_pool.json

# 按固定周期执行 5 轮并写账本
py -3.11 -m lbhealth run --config config/sample_pool.json --db artifacts/manual.sqlite --rounds 5

# 查看恢复结果、最新轮次、历史与快照
py -3.11 -m lbhealth status --config config/sample_pool.json --db artifacts/manual.sqlite

# 快照
py -3.11 -m lbhealth snapshot --config config/sample_pool.json --db artifacts/manual.sqlite --label manual --window 4
py -3.11 -m lbhealth snapshots --db artifacts/manual.sqlite

# 计算全量替换结果；可选输出恢复状态 JSON
py -3.11 -m lbhealth restore-snapshot --config config/sample_pool.json --db artifacts/manual.sqlite --snapshot-id 1 --state-out artifacts/restored.json

# 对当前已准备的后端重放快照窗口
py -3.11 -m lbhealth replay --config config/sample_pool.json --db artifacts/manual.sqlite --snapshot-id 1
```

`run` 默认按配置间隔固定节拍；测试时可加 `--no-fixed-period`。

## 界面

`lbhealth/ui.py` 提供 Tkinter 应用：

- 成员表稳定按配置顺序渲染：端点、协议、状态、连续成功/失败、慢启动进度、满额/生效权重、可用标记。
- 顶部实时显示轮次、可用成员集合、逐成员权重和权重和。
- 快照列表显示 ID、轮次、标签、配置版本、成员摘要和创建时间。
- 支持单轮探测、固定周期连续探测、Drain/Resume、保存快照、全量恢复快照。
- 事件区显示每轮探测、迁移、轮末结算和恢复摘要。

当前 Linux/WSL 环境默认 Python 没有安装 `tkinter`，因此额外使用本地解包的标准 Tk 8.6 库配合 WSLg 做了真实窗口冒烟，结果见 `VERIFICATION.md`。Windows 官方 Python 3.11 安装器自带 Tk，无需第三方包。

## 测试

```powershell
py -3.11 -m unittest discover -v
```

测试覆盖：

- 全部配置错误路径与 `E_POOL` 整体拒绝。
- 状态阈值、慢启动、恢复、Draining、历史不可变和轮末权重一致性。
- HTTP/TCP 真实套接字成功、503、真实超时和连接拒绝。
- complete/unknown/attempt 账本语义与强杀恢复。
- 快照全量替换、配置版本/成员摘要不一致、新增/移除成员和计数重置。
- 正向重放及探测、迁移、权重、配置、行为清单五类 `E_REPLAY` 定位。

本机实际结果见 `VERIFICATION.md` 和 `artifacts/demo_report.json`。
