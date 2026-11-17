# 故障隔离与容灾切换编辑台

面向 Windows 本地环境的领域数据编辑与操作应用。项目只使用 Python 标准库，不依赖 Docker、数据库服务或第三方包。场景以本地 JSON 为事实源，运行态、探测结论、状态迁移、切换决策与快照写入本地 SQLite；Tkinter 界面与 CLI 共用同一套引擎。

## 已实现能力

- **场景强校验**：服务/实例标识、主备角色、全局唯一端口、超时、重试、冷却、健康阈值和最小副本数；错误一次性汇总为 `E_SCENE`，每条包含行号与字段路径。
- **预算化并行探测**：全局线程预算与每服务信号量配额双重限制；真实 HTTP 探测区分 `success / unreachable / timeout / error`；每次尝试独立记录单调时钟起止毫秒，重试不覆盖旧结论。
- **健康状态机**：`unknown / healthy / unhealthy` 按连续成功/失败阈值迁移；迁移记录原因、轮次与时刻；无探测记录不会被推断为健康。
- **确定性容灾切换**：主实例不健康或健康副本不足时触发；备用目标按 `(instance_id, port, version)` 固定排序；SQLite 唯一事务行保证每服务仅一个进行中切换，并发请求收敛为唯一 `SWITCHED`，其余为 `E_BUSY`。
- **冷却与人工策略**：冷却窗口内记录 `E_COOLDOWN` 并抑制角色推进；`manual` 策略自动登记待确认请求并返回 `E_MANUAL_PENDING`，支持 CLI 人工批准。
- **切换后一致性校验**：重新探测整个服务，检查健康副本数、唯一主实例、源实例隔离标记；偏差以 `E_ISOLATE` 落库。普通探测不会清除隔离。
- **崩溃安全恢复**：SQLite WAL + `FULL` 同步；快照包含场景/策略版本、角色、实例版本、冷却起点、隔离、已完成切换、待确认请求及可重放轨迹。启动时整体替换快照，不增量合并；未完成事务或快照后未完成轮次的相关服务强制为 `unknown`。
- **重放校验**：从快照 `before` 状态、原决策时钟和故障注入重跑，逐项比较探测结论、状态迁移、切换目标、业务码；场景版本或策略/探测参数不一致时返回 `E_REPLAY` 及字段级差异。
- **异常注入**：CLI 可注入场景损坏 `E_SCENE`、实例不可达 `E_PROBE`、会话库写失败 `E_SNAPSHOT`，并写入 JSONL 结构化日志。

## 环境

- Python 3.11+
- 标准库：`tkinter`、`sqlite3`、`json`、`threading`、`socket`、`subprocess`
- Windows 官方 Python 安装时勾选 **tcl/tk and IDLE**。
- Linux 若缺少 Tk：`sudo apt install python3-tk`。

当前验证容器是 WSL/Linux，Python 3.11 可用，但未安装 `tkinter`；因此引擎、CLI、mock 服务、测试和三段演示均已实际运行，GUI 仅完成导入/无头降级验证。Windows 本地安装官方 Python 后可直接启动 GUI。

## 快速开始

```powershell
py -3.11 -m failover_console validate --scene scenes/sample_scene.json
py -3.11 -m failover_console check-ports --scene scenes/sample_scene.json
py -3.11 mock_cluster.py --scene scenes/sample_scene.json
```

另开终端执行一轮探测与切换：

```powershell
py -3.11 -m failover_console run `
  --scene scenes/sample_scene.json `
  --db artifacts/session.sqlite `
  --log artifacts/session.jsonl `
  --fault payments-b=unreachable
```

启动 Tkinter 编辑台：

```powershell
py -3.11 -m failover_console gui `
  --scene scenes/sample_scene.json `
  --db artifacts/session.sqlite `
  --log artifacts/session.jsonl
```

执行完整三段演示：

```powershell
py -3.11 demo.py
```

演示脚本会先检查样本端口；被占用时自动分配新的 `127.0.0.1` 高位端口并生成 `artifacts/demo/effective_scene.json`。结果写入 `artifacts/demo/result.json` 和结构化日志 `artifacts/demo/session.jsonl`。

## 场景格式

完整样本见 `scenes/sample_scene.json`。核心结构：

```json
{
  "scene_version": "demo-2026-11-17-v1",
  "probe_limits": {"global_concurrency": 4, "service_quota": 2},
  "services": [
    {
      "service_id": "payments",
      "policy": {
        "mode": "auto",
        "cooldown_ms": 5000,
        "min_healthy_replicas": 1,
        "version": "payments-policy-v1"
      },
      "instances": [
        {
          "instance_id": "payments-a",
          "role": "primary",
          "port": 47301,
          "probe": {
            "timeout_ms": 250,
            "retries": 1,
            "success_threshold": 1,
            "failure_threshold": 1
          }
        }
      ]
    }
  ],
  "faults": [
    {"instance_id": "payments-a", "kind": "unreachable", "note": "本地演示"}
  ]
}
```

约束：

- 服务标识、实例标识非空且唯一；每个服务恰有一个 `primary`。
- 端口为 `1..65535` 且全场景唯一；所有目标固定为 `127.0.0.1`。
- `timeout_ms`、`cooldown_ms`、两个健康阈值与 `min_healthy_replicas` 为正整数。
- `min_healthy_replicas <= 实例数`；`mode` 为 `auto | manual`。
- 故障类型为 `unreachable | timeout | error`，每个实例最多一个场景故障项。
- 未知字段也会被拒绝，避免拼写错误静默生效。

端口冲突时，可对所有参与进程使用相同映射：

```powershell
py -3.11 mock_cluster.py --scene scenes/sample_scene.json `
  --port-map payments-a=48301 --port-map payments-b=48302

py -3.11 -m failover_console run --scene scenes/sample_scene.json `
  --port-map payments-a=48301 --port-map payments-b=48302 `
  --db artifacts/session.sqlite
```

## CLI

| 命令 | 用途 |
| --- | --- |
| `validate` | 严格校验场景并输出指纹 |
| `check-ports` | 检查所有 `127.0.0.1` 端口是否可绑定 |
| `run` / `cycle` | 执行一轮并行探测、状态迁移、切换与一致性校验 |
| `restore` | 从最新快照整体恢复并显示状态 |
| `replay` | 重放最新快照并比较轨迹 |
| `status` | 显示恢复后的实例、策略和待确认请求 |
| `approve` | 人工确认待切换请求 |
| `gui` | 启动 Tkinter 编辑台 |

常用参数：

- `--fault INSTANCE=unreachable|timeout|error`：本轮运行时故障覆盖，可重复。
- `--inject scene-corruption`：在加载阶段注入损坏场景。
- `--inject unreachable --inject-instance ID`：注入实例不可达。
- `--inject snapshot-write-failure`：探测/切换照常推进，但快照写入报告 `E_SNAPSHOT`。
- `--now-epoch-ms`：固定决策时钟，用于确定性演示与冷却窗口验证。
- 所有业务错误以 JSON 输出，退出码为 `2`。

## 业务码

| 代码 | 含义 |
| --- | --- |
| `SWITCHED` | 切换完成且一致性校验通过 |
| `E_SCENE` | 场景整体拒绝，含 `line`、`field`、`message` |
| `E_PROBE` | 重试耗尽；单次尝试结论仍分别保留 |
| `E_COOLDOWN` | 冷却窗口内抑制重复切换 |
| `E_BUSY` | 同服务已有进行中切换事务 |
| `E_ISOLATE` | 切换后一致性偏差，含实例/字段明细 |
| `E_MANUAL_PENDING` | 人工策略拒绝自动切换并登记待确认 |
| `E_NO_TARGET` | 没有健康、未隔离的确定性备用目标 |
| `E_SNAPSHOT` | 会话库读写/快照失败 |
| `E_REPLAY` | 快照不可复现或轨迹差异 |
| `OK_REPLAY` | 重放逐项一致 |

## 关键不变量

1. 探测任务先获取全局执行预算，再获取本服务配额；重试全过程占用同一个服务配额槽。
2. 每个探测尝试立即写入 SQLite；状态与迁移记录使用同一事务提交。
3. 切换事务以 `switch_transactions.service_id` 主键互斥，角色与两侧实例版本在 `BEGIN IMMEDIATE` 中原子更新。
4. 目标候选只包含健康、未隔离的备用实例，并显式排序，不依赖字典/哈希顺序。
5. 冷却起点与角色更新同事务写入；后续重复请求只能得到 `E_COOLDOWN`。
6. 源实例隔离标记只由管理员显式清除，普通探测与状态恢复不会清除。
7. 启动恢复只采用 SQLite 快照整体替换；快照后的未完成事件使相关服务回到 `unknown`，等待重新探测。

更详细的表结构、调度和恢复设计见 [docs/DESIGN.md](docs/DESIGN.md)。

## 测试与验证

```powershell
py -3.11 -m unittest discover -v
py -3.11 demo.py
```

已在当前环境实际完成：

- `27` 项标准库 `unittest` 全部通过，覆盖强校验、四类探测、重试、并发预算、状态阈值、确定性目标、并发 `E_BUSY`、冷却、人工待确认、`E_ISOLATE`、隔离保持、快照整体替换、未完成事务恢复、无探测记录回归、重放差异与 CLI 损坏注入。
- 真实回环 HTTP 集群端到端切换与重放通过。
- 三段演示通过：全量探测/切换；冷却抑制；父进程快照后强制终止（Linux `SIGKILL`，退出码 `-9`），恢复后主实例保持在唯一目标，随后 `OK_REPLAY`。

最近一次实测输出保存在：

- `artifacts/demo/unittest_final.log`
- `artifacts/demo/demo_final.log`
- `artifacts/demo/result.json`
- `artifacts/demo/session.jsonl`

## 项目结构

```text
failover_console/
  scene.py          # JSON 行号定位与强校验
  probe.py          # 配额化并行探测、四类结论、HTTP 传输
  state_machine.py  # 健康状态阈值迁移
  repository.py     # SQLite 会话、事务互斥、快照恢复
  engine.py         # 切换决策、一致性校验、重放
  cli.py            # 命令行与异常注入
  gui.py            # Tkinter 编辑台（无 Tk 时安全降级）
scenes/             # 本地确定性场景
mock_cluster.py     # 仅监听 127.0.0.1 高位端口的模拟实例
demo.py             # 三段验收演示
tests/              # unittest 测试套件
```

## 安全边界

本项目只用于本地演示与测试，探测地址固定为 `127.0.0.1`，不访问外部目标，不使用容器。生产环境还需补充身份认证、配置签名、审计留存和更严格的进程级隔离。
