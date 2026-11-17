# 实际验证记录

验证日期：2026-11-17（Asia/Shanghai）

环境：WSL2 / Linux x86_64，Python 3.11.0rc1；标准库 `sqlite3`、`socket`、`subprocess` 可用。系统 Python 未安装 Tk，界面冒烟使用本地解包的标准 Tk 8.6 + WSLg 显示完成，未安装系统包。

## 1. 静态与单元测试

命令：

```bash
python3 -m compileall -q lbhealth scripts demo.py
python3 -m unittest discover -v
```

结果：编译通过；`Ran 28 tests`，`OK`（完整输出见 `artifacts/test_report.txt`）。

覆盖的实际断言包括：

- 非回环地址、重复 ID、非法端口/协议、阈值、权重、慢启动表、HTTP 路径注入均为 `E_POOL`，重复 ID 的字段路径保留原始数组下标。
- `unknown → healthy/unhealthy`、`healthy → unhealthy`、`unhealthy → healthy`、Drain/Resume 迁移和慢启动重置。
- 同轮状态迁移后使用轮末状态计算权重，例如第 2 轮 `alpha=15/60`、`bravo=0/40`，可用集仅为 `alpha`。
- 真实 HTTP `200`/`503`、真实 TCP `PONG`、连接拒绝，以及 350 ms 服务端延迟在 90 ms 客户端超时约束下产生 `failure_code=timeout`。
- 强杀模拟后只恢复 0 个 complete 轮次，未完成第 1 轮标记为 `unknown`，probes 表为 0；重跑后为 attempt 2 complete，且首次 unknown 历史仍保留。
- 快照配置版本和成员摘要不一致时执行 `full_replace`，新增成员重置、移除成员从状态中消失、计数重置列表可核验。
- 正/负重放分别验证全部一致和首个差异定位。

## 2. 三场景端到端演示

命令：

```bash
python3 demo.py --port-start 19461 --port-end 19520
```

结果：

```json
{
  "status": "ok",
  "scenarios": [
    {"name": "cold_start_convergence", "rounds": [1,2,3,4,5], "conclusion": "OK"},
    {"name": "fault_injection_transitions", "rounds": [6,7,8,9,10], "conclusion": "OK"},
    {"name": "forced_termination_recovery_replay", "rounds": [11,12,13,14], "conclusion": "OK"}
  ]
}
```

关键结果：

- 冷启动第 2 轮：`alpha unknown→healthy`、`bravo unknown→healthy`、`charlie unknown→unhealthy`、`delta unknown→unhealthy`；可用集 `[alpha, bravo]`，权重 `alpha=12, bravo=7`，和 19。
- 第 4 轮：`bravo healthy→unhealthy`；可用集 `[alpha]`，权重和 37。
- 冷启动第 5 轮：`alpha` 达到满额 50。
- 故障注入第 7 轮：`alpha healthy→unhealthy`；第 9 轮：`alpha unhealthy→healthy` 并重启慢启动；第 9/10 轮权重为 12/25，按轮末状态结算。
- 第 11 轮第一次 attempt 被强制终止并标记 `unknown`；第 11 轮第二次 attempt 从已提交的第 10 轮状态继续，完成 11–14 轮。
- 恢复报告为 `in_flight_results_recovered=false`，明确 unknown 轮次必须重跑。
- 快照窗口为 11–14 轮，重放基线为第 10 轮结束状态。
- 负向重放使用错误 `bravo` 行为，报告 `E_REPLAY`：`round=11`、`member=bravo`、`field=probe.ok`、期望 `true`、实际 `false`，依据为实际 `FAIL` 响应与快照 `PONG` 不一致。
- 恢复正确行为后，正向重放覆盖第 11–14 轮，状态迁移、可用集、权重与成员计数全部一致，返回 `OK`。
- 每次轮末都运行 `validate_round`，三个场景全部 `OK`，没有 `E_CONSISTENCY`。
- 三个场景顶层迁移数为 `5 / 3 / 0`（合计 8 条），每场景逐轮也保留完整探测、迁移、可用集与权重记录。
- 报告稳定核心字段的确定性摘要为 `ef45834f083a1e66127315f9e20822bbcf78c64419c69ef419dfea929da6d44d`。
- 在同一端口区间连续执行两次演示，确定性摘要完全相同，`repeatable=True`。

完整机器可读结果：`artifacts/demo_report.json`。

## 3. 账本验证

演示账本 `artifacts/run/ledger.sqlite` 中：

- 第 1–10、12–14 轮 attempt 1 为 `complete`。
- 第 11 轮 attempt 1 为 `unknown`，`effective_weight_sum` 为空，没有 probe 行。
- 第 11 轮 attempt 2 为 `complete`，`effective_weight_sum=67`。
- 共保存 56 条真实探测记录和 8 条状态迁移。
- 快照 ID 1，轮次 14，配置版本 `demo-v1`。

## 4. Tkinter 界面冒烟

命令（当前无系统 Tk 的 WSL 环境；Windows 直接使用 `py -3.11 -m lbhealth ui --smoke ...`）：

```bash
PYTHONPATH=... python3.11 -m lbhealth ui --smoke \
  --config config/sample_pool.json \
  --db artifacts/ui_ledger.sqlite
```

实际输出：

```json
{"config": "config/sample_pool.json", "ledger": "artifacts/ui_ledger.sqlite", "status": "ok", "ui": "opened"}
```

应用完整创建主窗口、配置/账本栏、成员表、快照表、事件区、控制按钮并正常关闭。演示恢复后的账本也可直接作为界面输入，展示第 14 轮状态与快照列表。

使用恢复后的账本再次冒烟：

```bash
PYTHONPATH=... python3.11 -m lbhealth ui --smoke \
  --config artifacts/run/config.json \
  --db artifacts/run/ledger.sqlite
```

输出：

```json
{"config": "artifacts/run/config.json", "ledger": "artifacts/run/ledger.sqlite", "status": "ok", "ui": "opened"}
```

随后执行 `status` 校验恢复视图：最新轮次 14、`requires_rerun=false`、快照数 1。机器可读结果保存在 `artifacts/cli_status.json`。
