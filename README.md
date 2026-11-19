# MixedPipe

面向训练/推理一体化场景的本地确定性数据处理管线与结果验证工具。系统在并行槽位、总计算预算和驻留内存预算约束下，对本地 JSONL 任务清单做可复现调度，通过 `spawn` 进程池执行确定性 NumPy 前向/后向模拟，并用串行基线独立验证并行结果。

不依赖 Docker、外部数据集、数据库或网络服务。运行时依赖仅为 Python 3.11+ 和 NumPy。

## 快速开始

### Windows PowerShell

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python scripts\generate_samples.py
python -m mixedpipe.cli demo
python -m mixedpipe.cli serve
```

浏览器打开 `http://127.0.0.1:8765`。Windows 下所有进程池均显式使用 `multiprocessing.get_context("spawn")`；请从项目根目录、经 `python -m mixedpipe.cli ...` 或有 `if __name__ == "__main__":` 保护的脚本入口运行。

### Linux / WSL

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python scripts/generate_samples.py
python -m mixedpipe.cli demo
python -m mixedpipe.cli serve
```

常用命令：

```bash
python -m mixedpipe.cli sample
python -m mixedpipe.cli run
python -m mixedpipe.cli validate
python -m mixedpipe.cli demo
python -m mixedpipe.cli serve --host 127.0.0.1 --port 8765
python -m pytest
```

`run`、`validate` 和 `demo` 均支持 `--manifest PATH` 与 `--config PATH`。

## 交付内容

- 确定性任务核心：按固定 seed 生成特征、目标和模型权重，训练执行前向、损失、确定性反向与 SGD 更新；推理只执行只读前向和打分。
- 三重预算调度：按“优先级降序 → 同 tag 推理共享收益降序 → 参数量升序 → `job_id` 升序”准入，支持确定性降级和结构化拒绝。
- 进程池执行：计划分组即进程池分块，组内并行、组间顺序执行，按计划顺序归并；异常按 `job_id + business_code + stage` 回传。
- 原子预算账本：准入扣减、失败释放和降级归还全部在单锁内结算，查询只看到完整快照。
- 版本化快照：计划、账本、结果各自携带版本号与 SHA-256 校验和，整文件原子替换；拒绝旧版本回退和损坏数据。
- 独立验证器：校验快照版本/交叉校验和、账本与准入集合、结果集合、并行摘要与按 `job_id` 升序的串行基线。
- 本地控制台：展示清单、裁决、预算、分组、摘要和最近业务错误，可触发演示与独立验证。

## 任务清单

每行一个 JSON 对象：

```json
{"job_id":"job-001","job_kind":"train","model_tag":"alpha","batch_size":4,"seq_len":8,"steps":3,"feature_dim":8,"seed":11,"priority":90}
{"job_id":"job-002","job_kind":"infer","model_tag":"alpha","batch_size":4,"seq_len":8,"warmup":3,"feature_dim":8,"seed":12,"priority":90}
```

| 字段 | 说明 |
| --- | --- |
| `job_id` | 全局唯一任务标识 |
| `job_kind` | `train` 或 `infer` |
| `model_tag` | 权重逻辑标识；同一 tag 只能使用同一 `feature_dim` |
| `batch_size`, `seq_len`, `feature_dim` | 张量形状，必须为正整数 |
| `steps` | 仅训练任务，正数 |
| `warmup` | 仅推理任务，正数 |
| `seed` | 非负整数，用于特征与目标生成 |
| `priority` | 整数，数值越大优先级越高 |
| `fault` | 可选演示字段：`load/forward/backward/update/score` |

固定样本由 `scripts/generate_samples.py` 生成：

- `data/jobs.jsonl`：任务清单。
- `data/tensors/features/*.npy`、`targets/*.npy`：按任务 seed 生成。
- `data/tensors/weights/*.npy`、`bias/*.npy`：按 `model_tag + feature_dim` 生成，同 tag 权重一致。
- `data/tensors/manifest.json`：形状、dtype 和数组内容校验和，供审计。

运行时也使用同一确定性生成函数从 seed 还原张量，因此不依赖上次运行留下的缓存状态。

## 调度与资源模型

主要配置位于 `configs/demo.json`：

| 字段 | 含义 |
| --- | --- |
| `max_slots` | 最大并行槽位数；0 时全部返回 `E_SLOT_BUDGET` |
| `compute_budget` | 计算单位总量 |
| `memory_budget_mb` | 准入集合驻留内存上限 |
| `learning_rate` | 确定性 SGD 步长 |
| `degradation_enabled` | 是否允许降级 |
| `degradation_priority_floor` | 可降级的最低优先级 |
| `min_batch_size`, `min_iterations` | 降级下限 |
| `equivalence_atol`, `equivalence_rtol` | 与串行基线比较的容差；默认 0 表示精确一致 |

估算采用整数运算，避免浮点决策漂移：

```text
params       = feature_dim^2 + feature_dim
weight_bytes = params * bytes_per_scalar
forward_ops  = 2 * batch_size * seq_len * feature_dim^2 + batch_size * seq_len * feature_dim
backward_ops = 2 * forward_ops + 2 * params

train_compute = steps * (forward_ops + backward_ops) + forward_ops
infer_compute = warmup * forward_ops
```

训练内存包含私有权重、梯度缓冲和激活；推理内存包含激活，且同 `model_tag` 的只读权重只计一次。完整公式见 `docs/DESIGN.md`。

降级顺序为“先按 1/2 缩小 `batch_size`，再按 1/2 缩小 `steps` 或 `warmup`”，直到配置下限；首个同时满足三重约束的组合生效。决策中记录原始/生效参数、类别、被降字段、每次尝试的估算和归还资源。

## 错误码

| 业务码 | 触发场景 |
| --- | --- |
| `E_COMPUTE_BUDGET` | 剩余计算预算无法容纳全量或降级任务 |
| `E_MEMORY_BUDGET` | 剩余驻留内存无法容纳全量或降级任务 |
| `E_SLOT_BUDGET` | 无并行槽位可容纳任务 |
| `E_STAGE_BUSY` | 已有调度提交生效，后到者立即失败 |
| `E_SNAPSHOT_CORRUPT` | 快照 JSON 损坏或校验和不符 |
| `E_STALE_VERSION` | 尝试覆盖已完成或更旧的快照版本 |
| `E_SNAPSHOT_VERSION_CONFLICT` | 三类快照版本或交叉校验和不一致 |
| `E_INJECTED_FAULT` | worker 内确定性故障注入 |
| `E_WORKER_INTERNAL` | worker 内未预期异常 |
| `E_WORKER_CRASH` | 进程基础设施异常，归因到具体任务 |
| `E_INCONSISTENT` | 结果、账本或串行基线不一致 |

worker 错误结果仍与准入集合一一对应，并包含 `job_id`、业务码、阶段和稳定消息；成功结果不会因同组另一任务失败而丢失。

## 交互控制台

`python -m mixedpipe.cli serve` 启动仅监听本机的标准库 HTTP 控制台：

- `GET /api/state`：清单、计划、账本、结果、分组和最近错误。
- `POST /api/run`：生成/加载固定样本并运行一次完整管线。
- `POST /api/validate`：独立运行账本、快照和串行基线验证。
- `GET /api/health`：健康检查。

固定演示包含 1 个 `E_COMPUTE_BUDGET` 拒绝（`job-008`）和 1 个 `E_INJECTED_FAULT@score`（`job-006`）。

## 测试与记录

```bash
python -m pytest
python -m mixedpipe.cli demo
```

当前实现覆盖 20 项自动测试，实际执行结果和样例校验和见 `docs/VERIFICATION.md`。

## 目录

```text
mixedpipe/          核心包：计算、调度、账本、执行、快照、验证、CLI、Web
configs/demo.json   固定演示预算配置
data/jobs.jsonl     固定本地任务清单
data/tensors/       固定 seed 张量与审计清单
scripts/            样本生成脚本
tests/              20 项验收测试
docs/               设计与实际验证记录
```
