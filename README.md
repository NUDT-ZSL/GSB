# 多阶段数据处理管线并行编排器

面向 Windows 本地环境的纯 Python 管线编排器。阶段脚本以本地子进程运行，通过
JSONL 文件与环境变量传递数据；调度器在全局资源预算与并发配额下并行推进
DAG，完整持久化会话快照，并提供独立验证、恢复重放与 Tkinter 界面。

## 环境准备

- Windows 安装 Python 3.11+（安装时勾选 Tcl/Tk，标准发行版自带 Tkinter）。
- 安装依赖：`python -m pip install -r requirements.txt`（仅 NumPy 与 pytest）。
- 不使用 Docker，不依赖网络或外部服务；样本由本地脚本生成。

## 目录结构

```
orchestrator/        核心包：DAG 校验、调度、执行、快照、验证、重放、UI
samples/stages/      确定性阶段脚本（含 NumPy 与失败注入）
samples/dags/        合法、失败注入、环依赖、紧预算 DAG
tests/               pytest 单元测试
docs/ARCHITECTURE.md 架构、状态机、业务码与恢复策略
demo.py              可重复三场景 + 负样本演示
run_demo.cmd / run_tests.cmd / run_ui.cmd  Windows 便捷脚本
runtime/             运行产出、会话快照与重放目录（可安全删除重建）
```

## 快速开始

```powershell
python samples\generate_samples.py
python -m orchestrator.cli validate-dag --dag samples\dags\pipeline_valid.json
python -m orchestrator.cli run --dag samples\dags\pipeline_valid.json `
  --workspace runtime\work\success --snapshot runtime\sessions\success.json
python -m orchestrator.cli verify `
  --snapshot runtime\sessions\success.json --workspace runtime\work\success
python -m orchestrator.cli replay --dag samples\dags\pipeline_valid.json `
  --snapshot runtime\sessions\success.json --replay-root runtime\replay\success
python -m orchestrator.cli ui --snapshot runtime\sessions\success.json
```

一键演示（顺序稳定输出全部报告）：

```powershell
run_demo.cmd
```

## 样本 DAG

| 文件 | 用途 |
| --- | --- |
| `pipeline_valid.json` | 五阶段合法 DAG，含强/弱依赖，预算 4 |
| `pipeline_failure.json` | 打开重试配置，配合失败注入验证重试与 skip 传播 |
| `pipeline_cycle.json` | 含环依赖，加载即被 `E_DAG` 拒绝 |
| `pipeline_tight_budget.json` | 预算 2，确定性触发资源等待队列 |

失败注入通过 CLI 传入，例如：

```powershell
python -m orchestrator.cli run --dag samples\dags\pipeline_failure.json `
  --workspace runtime\work\fail --snapshot runtime\sessions\fail.json `
  --fail-first-n "validate=1,aggregate=3"
```

含义：`validate` 第 1 次尝试失败后重试成功；`aggregate` 连续 3 次失败
（重试 1 次后仍失败），其强下游 `report` 被标记 `skipped`。

## 业务码

| 业务码 | 含义 | 退出码 |
| --- | --- | --- |
| `E_DAG` | DAG 结构/字段/环校验失败 | 2 |
| `E_VERIFY` | 快照与产出/预算/依赖顺序校验偏差 | 3 |
| `E_REPLAY` | 恢复重放不可复现 | 4 |
| `E_SNAPSHOT` | 快照损坏或版本不支持 | 5 |

## 演示场景

`demo.py` 固定顺序执行：

1. 全量成功运行 + 独立验证（全部检查 `passed`）。
2. 失败注入、逐尝试记录、弱依赖标注与强依赖 skip 传播 + 验证。
3. 硬崩溃恢复：仅恢复已完成阶段，未完成标记 `unknown`，再做确定性重放。
4. 紧预算等待队列，以及环 DAG、篡改产出、版本漂移、无头 UI 负样本。

## 实际验证结果

在本地 Python 3.11 环境执行：

```
python -m pytest tests -q
31 passed
python demo.py
demo exit=0
```

已验证要点：

- 环 DAG 返回 `E_DAG` 并给出环路径与字段位置。
- 全量成功管线资源峰值等于预算 4 且从未超预算。
- 失败场景中 `aggregate` 记录 2 次尝试后失败，`report` 带 `skipped_by=aggregate`。
- 紧预算 DAG 记录 `resource_budget_unavailable` 等待事件，峰值受预算约束。
- 篡改输出文件后验证器稳定列出 `sha256_mismatch` 与记录数偏差，返回 `E_VERIFY`。
- 脚本或 DAG 漂移触发指纹不一致，重放返回 `E_REPLAY`；正常重放全部 `sha256_match`。

说明：当前容器未安装系统级 Tkinter，因此界面以无头视图模型方式冒烟验证；
在 Windows 标准 Python（含 Tcl/Tk）上 `ui` 命令直接打开图形窗口。
