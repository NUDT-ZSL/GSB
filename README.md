# Replica Ledger

面向 Windows 本地环境的分布式副本修复账本组件。组件从一个严格校验的副本组 JSON 出发，通过回环 TCP JSON-lines 协议读取各节点块摘要，按确定性多数规则选出真值副本，以整数定点估算失配比例与修复成本，生成可解释的修复任务，并将修复前/后分布、真值结论、成本和格度翻转写出为 JSON + SQLite 账本。

核心业务计算全部使用整数和 `fractions.Fraction` 诊断值，不使用浮点数参与判定或排序。`numpy` 按任务环境要求安装，但业务判定不依赖 NumPy 浮点行为。

## 快速开始（Windows）

所有命令在项目根目录执行；需要 Python 3.11+ 的 Windows 官方安装包（包含 Tkinter）。

```bat
scripts\setup_windows.bat
scripts\check_ports.bat
scripts\generate_samples.bat
scripts\run_demo.bat
scripts\run_tests.bat
scripts\launch_gui.bat
```

启动和终止本地示例节点：

```bat
scripts\start_all_nodes.bat
scripts\stop_nodes.bat
```

`start_node.bat` 也可单独使用：`scripts\start_node.bat samples\scenarios\multi\spec.json n1 19101`。PID、端口和恢复信息写入 `run\node-*.json`；强制停止后重启不会声称存在退出报告，只扫描持久化块文件和协议 JSONL 记录。

## 快速开始（Linux/WSL 验证环境）

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m replica_ledger.cli generate --output samples --force
.venv/bin/python -m replica_ledger.cli demo --workdir run/demo
.venv/bin/python -m unittest discover -s tests -v
```

当前交付环境缺少 Tkinter，因此自动化验证覆盖 GUI 的数据模型和稳定排序；Windows 官方 Python 安装包可直接运行 GUI。

## 已生成样本

`scripts/generate_samples.py` 生成三个确定性场景，均由本地哈希流构造，不读取网络或附件：

- `samples/scenarios/single`：4 个副本，1 个失配副本，用于单副本修复。
- `samples/scenarios/multi`：6 个副本，4 个真值副本、2 个失配副本，用于多数修复。
- `samples/scenarios/grid`：包含 1/6 失配比例告警与 3:3 摘要频次并列，用于格度告警翻转、优先级决胜和修复源排序翻转。

样本块文件按逻辑块标识哈希命名，避免 Windows 文件名保留字符；逻辑映射完全由 `spec.json` 决定。

## 主要命令

```bash
# 严格校验 spec；失败统一返回 E_SPEC 及字段路径
python -m replica_ledger.cli validate --spec samples/scenarios/grid/spec.json

# 摘要、真值、失配比例、修复任务与成本
python -m replica_ledger.cli check --spec samples/scenarios/single/spec.json --scale 8 --grid-scales 8,16

# 格度翻转诊断
python -m replica_ledger.cli grid --spec samples/scenarios/grid/spec.json --scales 3,4,6,8,16

# 三次完整实验与稳定报告
python -m replica_ledger.cli demo --workdir run/demo

# 启动单个回环节点或 Tkinter 诊断界面
python -m replica_ledger.cli serve --spec samples/scenarios/multi/spec.json --node-id n1 --port 19101
python -m replica_ledger.cli gui --spec samples/scenarios/grid/spec.json --scale 8
```

## 三类异常注入

注入标志只修改输入数据文件或输入参数，不修改源码。建议先复制场景目录再运行；直接使用仓库内样本会在原文件上截断/改摘要。

```bash
# E_BLOCK：截断输入块文件
python -m replica_ledger.cli check --spec COPY/spec.json --scale 8 --inject-block-truncate n4:B-SINGLE --log logs/e_block.jsonl

# E_INTEGRITY：改写输入 spec 的合法十六进制分片摘要
python -m replica_ledger.cli check --spec COPY/spec.json --scale 8 --inject-digest-corrupt n1:B-SINGLE --log logs/e_integrity.jsonl

# E_GRID：使用非法格度参数
python -m replica_ledger.cli check --spec samples/scenarios/single/spec.json --scale 8 --inject-grid --log logs/e_grid.jsonl
```

退出码：`E_SPEC=2`、`E_BLOCK=3`、`E_INTEGRITY=4`、`E_GRID=5`、`E_PROTOCOL=6`、`E_LEDGER=7`。结构化日志为 JSONL，每条异常至少包含 `code`、`field_path`、`node_id`、`block_id`。

## Python API

主要公开接口位于 `replica_ledger/__init__.py`：

```python
from replica_ledger import (
    load_replica_group,
    analyze_local,
    diagnose_grid,
    LedgerStore,
    build_ledger_payload,
)

spec = load_replica_group("samples/scenarios/single/spec.json")
before = analyze_local(spec, scale=8)
diagnosis = diagnose_grid(spec, scales=[8, 16])

for task in before.plan.tasks:
    print(task.block_id, task.source_node_id, task.target_node_id,
          task.transfer.value, task.transfer.error_bound_units,
          task.cost_fixed, task.cost_error_bound_units)
```

完整 TCP 修复、账本保存与重开示例见 `examples/api_integration.py`：

```bash
python examples/api_integration.py
```

## 定点数学与判定边界

对非负有理数 `n/d` 和整数格度 `S`，所有量化均向下截断：

```text
q = floor(S * n / d)
quantization_error = S*n/d - q = remainder/d,  0 <= error < 1 个定点单位
```

失配比例与告警阈值使用同一格度量化。量化差 `G = q_ratio - q_threshold` 的真实误差严格位于 `(-1, 1)` 个定点单位，因此：

- `G >= 1`：可确定真实比例严格大于阈值；
- `G <= -1`：可确定真实比例不大于阈值；
- `G == 0`：标记 `indeterminate`，不伪造确定结论；唯一例外是失配数恰为 0 时可直接由精确整数关系判定 `not_above`。

成本包含传输、工作量、优先级惩罚和负载惩罚四个定点项。若两个候选成本差不超过双方派生误差上界之和，则进入并列通道，按“优先级降序、节点标识升序”决胜。翻转报告输出前后判定、块标识、目标节点、成本项、误差界、实际差值与 `error_bound_share_ppm`。

精确摘要多数结论和精确失配集合与格度无关；格度只会改变比例告警的确定性状态和误差感知下的成本排序。该不变量在诊断和测试中显式检查。

## 账本语义

- JSON 账本包含整数 `format_version`、`content_digest` 与完整 `payload`；`content_digest` 是规范化 payload 的 SHA-256。
- SQLite 同时保存完整规范化 payload 及分布、真值、修复任务、成本项、格度翻转表。
- 重新打开时调用 `LedgerStore.open(expected_group_state_digest=...)` 比较格式版本与当前副本组状态摘要；不一致返回 `stale`，不进行字段级合并。
- 调用 `reconcile` / `restore_or_replace_ledger` 时，新视图整体写入，旧 JSON 与 SQLite 复制为只读备份，命名为 `ledger.backup-<content_digest前缀>.*`。
- 修复前状态摘要、修复任务、成本、修复后分布和修复后真值均在同一账本视图中保留。

## 目录结构

```text
replica_ledger/       领域组件、协议、账本、GUI、CLI
scripts/              样本生成、演示、Windows 启停与测试批处理
samples/              确定性生成样本
tests/                unittest 自动化测试
examples/             可集成 API 示例
docs/                 架构、协议、定点误差与验证记录
reports/              稳定演示报告
```

## 验证状态

当前环境实际验证：Python 3.11.0rc1、NumPy 2.4.6，30 项 unittest 全部通过；三次演示均完成 TCP 修复，账本重开为 `current`，重复演示报告字节完全一致。详细输出与命令见 `docs/VALIDATION.md`。
