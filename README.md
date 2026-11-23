# Elastic Scalpel

本地模型推理服务弹性伸缩控制面与可视化诊断工作台。项目在确定性样本上复现多轮伸缩、GPU/并发双预算装箱、副本生命周期、指数退避、SQLite 事务快照和强制终止后的恢复一致性。

## 核心能力

- 严格校验服务、节点、服务级负载轨迹与伸缩配置；非法输入在写事务前失败。
- 使用 `Decimal` 计算 `ceil(arrival_rate * avg_sequence_length / replica_capacity)`，记录观测项、聚合顺序、取整位置和上下限裁剪。
- 节点级显存与并发实例双预算原子装箱，记录 GPU 余额、淘汰候选和首个约束。
- 按服务分组并发拉起，遵守每服务并发上限和每轮逻辑时间预算；区分预算内未启动、拉起后失败、主动放弃。
- 副本状态机：`PENDING → COLD_STARTING → READY → DRAINING → RECYCLED`，失败副本回到 `PENDING` 并按逻辑时钟退避。
- 拉起前事务性提交操作意图和预算/资源预占；拉起完成后提交结果，恢复时不重复扣预算。
- SQLite WAL + 进程文件锁 + 快照 CAS 指针；每轮生成新快照并整体切换 `current_snapshot`。
- 本地回环 HTTP 拉起桩支持幂等操作查询、失败、慢响应、首次失败、健康探测失败。
- 诊断工作台展示副本阶段、节点余额、轮次预算、装箱证据、放弃原因、退避计划和恢复审计。

## 环境

要求 Python 3.11+、Flask、NumPy。实际验证环境为 Python 3.11.18、Flask 3.1.3、NumPy 2.4.6、pytest 9.2.0。

Linux/macOS：

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python --version
```

Windows 命令行：

```bat
py -3.11 -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python --version
```

本工作区已创建 `.venv`，可直接使用 `.venv/bin/python`。

## 快速运行

打开两个终端，第一个启动本地拉起桩：

```bash
.venv/bin/python -m scaler stub --port 18765 --scenario samples/scenario.json
```

第二个终端初始化并运行三轮：

```bash
.venv/bin/python -m scaler init --db data/scaler.sqlite
.venv/bin/python -m scaler run --db data/scaler.sqlite --rounds 3
.venv/bin/python -m scaler status --db data/scaler.sqlite
```

启动可视化工作台：

```bash
.venv/bin/python -m scaler serve --db data/scaler.sqlite --port 8080
```

浏览器打开 `http://127.0.0.1:8080/`。

`status`、`recover` 只依赖 SQLite 中保存的规格、快照和操作意图，不要求原始 JSON 文件仍然存在。

## 强制终止演练

脚本会启动内嵌回环桩，先完成一份无中断基线，再在指定轮次、指定已派发操作数后向控制面进程发送强制终止，随后执行 `recover` 并补齐剩余轮次。最后逐字段比较基线与恢复状态。

```bash
.venv/bin/python scripts/chaos_drill.py \
  --rounds 3 \
  --fault-round 2 \
  --fault-after-dispatch 2 \
  --fast-delays \
  --run-id final-drill
```

输出 `state_equal: true` 表示恢复状态与无中断运行完全一致。报告和两份状态快照写入：

```text
artifacts/chaos-drill/final-drill/report.json
artifacts/chaos-drill/final-drill/baseline_state.json
artifacts/chaos-drill/final-drill/recovered_state.json
```

也可以手工演练：

```bash
# 子进程会在第 2 轮派发 2 个操作后被杀，退出码为 137（POSIX 下可能是 -9）
.venv/bin/python -m scaler run --db data/crash.sqlite --rounds 2 \
  --fault-round 2 --fault-after-dispatch 2

.venv/bin/python -m scaler recover --db data/crash.sqlite
.venv/bin/python -m scaler status --db data/crash.sqlite
```

## CLI

| 命令 | 作用 |
| --- | --- |
| `init` | 校验输入并初始化 SQLite 快照 |
| `run --rounds N` | 运行确定性伸缩轮次 |
| `recover` | 查询并裁决持久化的在途操作，不重新预占预算 |
| `status` | 输出稳定排序状态 JSON、事件、恢复审计和快照链 |
| `stub` | 启动本地回环 HTTP 拉起桩 |
| `serve` | 启动 Flask 诊断工作台 |
| `demo` | 启动临时桩并跑样本轮次 |

通用参数：`--services`、`--nodes`、`--trace`、`--config`、`--db`。`run` 还支持 `--start-clock-ms`、`--service`、`--request-id`、`--fault-round`、`--fault-after-dispatch`。

## 数据契约

服务示例：

```json
{
  "name": "chat-alpha",
  "model_version": "llm-2026.11",
  "replica_memory_mib": 8192,
  "replica_compute_units": 2,
  "tokens_per_second_per_compute_unit": 300.0,
  "min_replicas": 1,
  "max_replicas": 5,
  "cold_start_ms": 250,
  "batch_window_ms": 100,
  "topology_domains": ["zone-a"],
  "priority": 20
}
```

单副本吞吐能力为 `replica_compute_units * tokens_per_second_per_compute_unit`。负载轨迹按服务提供：

```json
{
  "services": {
    "chat-alpha": [
      {
        "logical_time_ms": 1000,
        "arrival_rate": 2.0,
        "avg_sequence_length": 512.0
      }
    ]
  }
}
```

旧的扁平 `{"windows": [...]}` 格式仍可作为所有服务的默认序列使用。每个窗口取逻辑时钟之前最近的一条观测。

配置示例：

```json
{
  "config_version": 1,
  "round_budget_ms": 600,
  "per_service_launch_limit": 2,
  "backoff_base_ms": 200,
  "backoff_max_ms": 2000,
  "failure_threshold": 3,
  "jitter_rule": {"mode": "deterministic", "percent": 20},
  "clock_step_ms": 1000,
  "clock_regression_tolerance_ms": 0,
  "drain_duration_ms": 400,
  "health_probe_enabled": true,
  "launch_target_url": "http://127.0.0.1:18765"
}
```

`launch_target_url` 只允许 `localhost`、`127.0.0.1`、`::1`，不会访问外部网络。

## 确定性规则

目标副本数：

```text
capacity = replica_compute_units * tokens_per_second_per_compute_unit
demand   = arrival_rate * avg_sequence_length
target   = clamp(ceil(demand / capacity), min_replicas, max_replicas)
```

使用十进制字符串构造 `Decimal`，输入优先级固定为：服务优先级降序、服务名升序、副本序号升序。退避抖动只使用 `SHA-256(service|replica|kind|streak|delay)` 的稳定整数桶，不读取系统墙钟。

完整设计与恢复协议见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)，实际验证记录见 [docs/VERIFICATION.md](docs/VERIFICATION.md)。

## 错误码

| 错误码 | 场景 |
| --- | --- |
| `E_INPUT_INVALID` | 字段类型、范围、格式非法 |
| `E_DUPLICATE_SERVICE` / `E_DUPLICATE_NODE` | 名称重复 |
| `E_DUPLICATE_WINDOW_TIME` | 同一服务窗口逻辑时间重复 |
| `E_CLOCK_REGRESSION` | 逻辑时钟回拨超过容差 |
| `E_NO_OBSERVATION` | 当前逻辑时钟没有可用观测 |
| `E_CONFIG_VERSION_REGRESSION` | 配置版本回退 |
| `E_CONFIG_VERSION_CONFLICT` | 相同配置版本对应不同内容 |
| `E_LEDGER_MISMATCH` | 显存/并发账本与副本占用不守恒 |
| `E_LAUNCH_CONNECTION_REFUSED` | 拉起目标拒绝连接 |
| `E_COLD_START_BUDGET_EXCEEDED` | 单副本冷启动响应超过逻辑预算 |
| `E_LAUNCH_HTTP_ERROR` | 拉起目标 HTTP/超时错误 |
| `E_LAUNCH_RESPONSE_INVALID` | 拉起目标响应非法 |
| `E_STATE_VERSION_CONFLICT` | 快照 CAS 失败 |
| `E_UNSAFE_SPEC_CHANGE` | 有活动副本时变更服务/节点拓扑 |
| `E_REQUEST_CONFLICT` / `E_REQUEST_IN_PROGRESS` | 幂等键内容冲突或尚未完成 |

拉起类错误不会丢弃整轮状态：它们会出现在轮次的 `error_codes`、`dispositions[*].reason_code` 和副本退避记录中。配置/账本类错误则拒绝整个事务，快照指针保持不变。

## 测试

```bash
.venv/bin/python -m pytest
```

当前测试覆盖校验、确定性换算、双预算装箱、六轮生命周期、健康失败排空、预算分类、请求幂等、连接拒绝、冷启动超标、账本篡改、工作台 API、多进程并发和强制终止恢复。

## 项目结构

```text
scaler/
  models.py        数据模型与校验
  conversion.py    确定性目标副本换算
  packing.py       显存/并发双预算装箱
  backoff.py       逻辑时钟指数退避
  storage.py       SQLite 快照、WAL、文件锁、CAS
  state.py         状态机与账本守恒不变量
  engine.py        预占、调度、裁决、恢复
  launch_client.py 回环 HTTP 幂等客户端
  stub.py          本地拉起/健康桩
  workbench.py     Flask API
  templates/       可视化工作台
samples/           确定性服务、节点、轨迹、配置、故障场景
scripts/           chaos_drill.py 强制终止演练
tests/             自动化测试
```

## 边界说明

- GPU、推理服务和网络均为本地确定性模拟，不代表真实硬件调度性能。
- 快照保留最近 50 轮核心历史，完整事件和恢复审计保留在 SQLite 事件表。
- Linux 环境已完成实际强杀与恢复验证；Windows 分支包含 `msvcrt` 文件锁和 `os._exit(137)` 强制退出回退，但未在 Windows 实机执行。
