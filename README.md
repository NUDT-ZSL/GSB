# dynabatch

`dynabatch` 是一个完全本地、确定性、可崩溃恢复的推理服务动态批处理调度与诊断引擎。
它不加载真实模型、不访问网络、不依赖 GPU 或容器；模型算力和显存开销由确定性解析公式估算。

核心能力：

- 严格校验模型、副本版本、请求轨迹、定点标度、容差和调度预算。
- 仅使用注入的逻辑时钟推进窗口，不读取墙钟。
- 副本窗口绑定开始时版本，窗口到期即使未满批也提交。
- 在最大批大小、算力、显存、并发和到期时间约束下裁决批次。
- 记录优先级插队、被跳过请求、首个触发约束、否决原因和资源观测。
- 每次定点换算都记录原始浮点、标度、舍入方向和误差上界。
- 两个候选批次或副本的估算差小于容差且影响裁决时记录精度敏感标记。
- SQLite WAL + 文件锁 + `BEGIN IMMEDIATE` 事务，支持多进程幂等提交。
- 准备提案和最终提交分段，可在两个边界强制终止并继续计算。
- 不中断运行与崩溃恢复运行的 `status` JSON 字节完全一致。

## 环境

要求 Python 3.10 及以上。Windows PowerShell 示例：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

离线 wheel 目录：

```powershell
.\.venv\Scripts\python.exe -m pip install --no-index --find-links .\wheels -r requirements.txt
```

SQLite 由标准库 `sqlite3` 提供。引擎初始化时强制使用 WAL；`verify` 会重新检查
`PRAGMA integrity_check` 和 `journal_mode=wal`。

Linux/macOS：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

## 快速开始

生成固定种子样本及摘要：

```bash
python scripts/generate_sample.py
```

Linux/macOS：

```bash
python -m dynabatch save --bundle sample/bundle.json --db state.db
python -m dynabatch schedule --db state.db --now 300
python -m dynabatch train --db state.db --now 300
python -m dynabatch status --db state.db --output status.json
python -m dynabatch verify --db state.db
```

Windows PowerShell 将 `python` 换成 `.\.venv\Scripts\python.exe` 即可。

安装包后也可直接使用 `dynabatch ...`。未安装时始终可用 `python -m dynabatch ...`。

## 子命令

| 命令 | 作用 |
| --- | --- |
| `save` | 校验并持久化完整 bundle；非法输入返回业务错误码且不创建数据库 |
| `schedule` | 推进窗口、组装并提交批次；支持 `--now` 和 `--crash-at` |
| `train` | 对已提交批次做确定性模拟执行、超期判定和失败注入 |
| `resume` | 先完成遗留提案，再继续调度未完成窗口 |
| `status` | 稳定排序输出逻辑水位、窗口、请求、批次、误差谱和敏感标记 |
| `verify` | 检查 SQLite、WAL、唯一归属、资源账本、误差账本等不变量 |

`resume` 未指定 `--now` 时使用 SQLite 中已提交的逻辑时钟水位；指定更大的
`--now` 才会继续向未来推进。逻辑时钟单调递增，向后注入会被水位钳制。

常用参数：

```text
--bundle PATH              JSON bundle（也可作为位置参数）
--db PATH                  状态数据库，默认 dynabatch.db
--now N                    注入逻辑时钟（毫秒），不使用系统墙钟
--crash-at batch_decision  提案已提交、窗口尚未最终提交时硬终止，退出码 86
--crash-at window_commit   最终提交事务内、COMMIT 前硬终止，退出码 87
--fail-batch BATCH_ID      train 时注入真实执行失败，可重复
--baseline PATH            verify 时对比另一个状态库
--canonical                status 输出紧凑规范 JSON
```

## 数据契约

`sample/bundle.json` 是完整可运行示例。主要结构：

```json
{
  "schema_version": 1,
  "model": {
    "model_id": "tiny",
    "layers": [{"name": "encoder", "parameters": 2000000}],
    "input_tokens": 12,
    "output_tokens": 8,
    "bytes_per_parameter": 2,
    "activation_bytes_per_token": 8192,
    "latency_ms_per_compute_unit": 10.0
  },
  "replica_versions": [
    {
      "version": 1,
      "effective_at_ms": 0,
      "replicas": [
        {
          "replica_id": "r0",
          "compute_units": 1.0,
          "memory_mib": 64,
          "batch_window_ms": 20,
          "max_batch_size": 3,
          "max_sequence_length": 64,
          "max_concurrent_batches": 2
        }
      ]
    }
  ],
  "requests": [
    {
      "seq": 1,
      "arrival_ms": 0,
      "priority": 2,
      "input_tokens": 8,
      "max_output_tokens": 6,
      "deadline_ms": 100
    }
  ],
  "precision": {
    "compute_scale": 1000000,
    "memory_scale": 1024,
    "latency_scale": 1000,
    "tolerance": 0.35,
    "rounding": "half_up"
  },
  "scheduler": {
    "logical_time_budget_ms": 60,
    "max_concurrent_batches": 4
  },
  "execution": {
    "mode": "deterministic",
    "fail_batches": [],
    "latency_overhead_ms": 1
  }
}
```

校验错误码包括 `E_REPLICA_DUPLICATE`、`E_REPLICA_COMPUTE`、`E_REPLICA_MEMORY`、
`E_REPLICA_BATCH_SIZE`、`E_SEQUENCE_LENGTH`、`E_REQUEST_DUPLICATE`、
`E_REQUEST_TIME`、`E_REQUEST_DEADLINE`、`E_PRECISION_SCALE`、
`E_PRECISION_TOLERANCE`、`E_VERSION_ROLLBACK`、`E_VERSION_CONFLICT` 等。
CLI 将错误以稳定 JSON 写入 stderr，并返回退出码 2。

## 确定性估算

设模型总参数量为 `P`，请求输入和最大输出 token 之和为 `T`：

```text
request_compute = 2 * P * T / 1e9
batch_compute   = sum(request_compute)
weights_mib     = P * bytes_per_parameter / 1048576
batch_memory    = weights_mib + sum(T * activation_bytes_per_token / 1048576)
latency_ms      = mean(window_end - arrival) + batch_compute * latency_factor
```

算力预算按“每窗口”使用副本 `compute_units`；显存上限按单批峰值检查；
批数量上限取副本并发上限与全局 `max_concurrent_batches` 的较小值。

定点换算使用 `Decimal` 和显式舍入模式，避免平台浮点格式化差异。每个换算项记录：

- 换算前原始浮点值；
- 定点标度；
- 定点整数；
- 舍入方向 `up` / `down` / `exact`；
- 该操作误差界与累计误差界。

乘法和加法均进入 `error_operations`。候选差值严格满足 `difference < tolerance`
时才可能成为精度敏感项；同时必须记录 `depended=true` 及具体裁决原因。

## 窗口、请求与版本

- 请求初始状态 `PENDING`，窗口提交后 `BATCHED`，模拟执行经 `RUNNING` 到
  `COMPLETED` 或 `EXPIRED`。
- `request_assignments` 保证一个请求最多一个当前窗口归属；批次成员表和
  `(request_seq, replica_version)` 唯一约束阻止同版本重复入批。
- 窗口按 `floor(window_start / batch_window_ms) * batch_window_ms` 对齐。
- 窗口 ID 含副本 ID、版本和起止时钟，例如 `w:r-a:v1:0-20`。
- 版本在窗口开始时由 `effective_at_ms <= window_start` 选择，并写入窗口；
  窗口结束后也不会切换到新版本。
- 新版本必须整体存储快照并移动 current 指针。相对上一版分为
  `noop`、`incremental_append`、`complete_replace`；低于当前版本的提交被拒绝。

## 恢复协议

窗口提交分成两个持久阶段：

1. **准备提案**：在一个事务中写入 `windows.status=proposed`、`batches.status=proposed`、
   批次成员、误差账本和敏感标记。
2. **最终提交**：另一个事务将批次和窗口改为 `committed`，更新请求状态，并
   `INSERT OR IGNORE` 资源扣减记录。资源剩余量从账本聚合，不依赖内存计数器。

两个 SQLite 事务均为原子操作。进程在阶段 1 之后、阶段 2 之前退出时，恢复路径
直接完成既有提案；在阶段 2 的 `COMMIT` 前退出时，事务回滚，恢复路径重放同一提案。
因此算力/显存不会被重复扣减，超期计数也不会重复推进。

## 验收

```bash
python -m pytest -q
python scripts/run_acceptance.py
```

验收脚本会自动运行：

1. 不中断：`save -> schedule -> train -> status -> verify`；
2. 在 `batch_decision` 结束进程；
3. 在 `window_commit` 结束进程；
4. 两种崩溃均执行 `resume -> train -> verify --baseline`；
5. 比较 `status --canonical` 的原始字节、状态摘要、批次和资源账本。

本仓库实际运行结果见 `artifacts/acceptance-report.json`。当前结果：

- pytest：15 项通过；
- 两个崩溃注入退出码分别为 86、87；
- 两条恢复路径的 `status` 文件 SHA-256 与不中断基线相同；
- 恢复后 `verify --baseline` 均通过。

## 项目结构

```text
dynabatch/                 引擎实现
  validation.py            数据契约与业务错误码
  fixed.py                 定点换算和误差谱
  scheduler.py             窗口、归属、组批、两阶段提交
  training.py              确定性执行与生命周期
  status.py                稳定状态投影与不变量验证
  store.py                 SQLite/WAL/事务/版本快照
  locking.py               Windows/Linux 跨平台文件锁
scripts/generate_sample.py 固定种子样本生成
scripts/run_acceptance.py  双崩溃边界验收
sample/                    确定性样本与摘要
tests/                     单元、并发和恢复测试
```
