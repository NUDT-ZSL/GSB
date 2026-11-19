# 本地数据处理管线并行调度与结果验证工具

在确定性样本上复现资源预算约束下的并行调度决策。纯 Python 标准库实现
（sqlite3 / threading / concurrent.futures），无 Docker、无外部服务。

## 环境

- Python 3.11+（实测 3.12.13），`pip install numpy pytest`
- SQLite 由标准库 `sqlite3` 提供
- 路径全部通过 `pathlib` 处理，Windows 本地路径（如 `C:\data\ledger.db`）与
  POSIX 路径均可直接使用

## 结构

- `pipeline_tool/pipeline.py` — 管线描述解析与校验，依赖图与阶段分组
- `pipeline_tool/scheduler.py` — 拓扑调度器（阶段顺序 + 并发/内存双预算）
- `pipeline_tool/operators.py` — 确定性算子（filter / aggregate_int / aggregate_float）
- `pipeline_tool/store.py` — SQLite 账本（节点状态、阶段水位、逻辑时钟）、
  跨进程文件锁、结果文件原子写
- `pipeline_tool/verify.py` — 账本 / 结果文件 / 追溯映射一致性校验
- `pipeline_tool/cli.py` — `run` / `resume` / `status` / `verify` 子命令
- `data/` — 固定样本：`pipeline.json`（含预算放弃场景）、
  `pipeline_crash.json`（强杀恢复场景）、`input_records.json`
- `tests/` — 25 个 pytest 用例

## 使用

```bash
python -m pipeline_tool run    --pipeline data/pipeline.json --input data/input_records.json --db artifacts/run1.db --out artifacts/run1.json
python -m pipeline_tool resume --pipeline data/pipeline_crash.json --input data/input_records.json --db artifacts/crash.db --out artifacts/crash.json
python -m pipeline_tool status --db artifacts/run1.db
python -m pipeline_tool verify --pipeline data/pipeline.json --input data/input_records.json --db artifacts/run1.db --out artifacts/run1.json
```

`run` 会清除旧状态重新开始；`resume` 依据 SQLite 账本只补算未完成节点。

## 关键设计

- **调度**：按阶段序号推进；阶段内以 `(-memory_mib, name)` 确定顺序启动，
  受 `max_concurrency` 与 `memory_budget_mib` 双重约束。单节点内存超过阶段
  预算立即放弃；阶段结束仍未启动的节点显式放弃并记录原因。节点结局三分：
  `FAILED`（已启动后失败）、`ABANDONED`（主动放弃）、`PENDING`（预算内未启动，
  仅出现在管线中断时）。已启动节点执行到完成或明确失败，不使用超时截断。
- **确定性**：输入记录按 id 排序；整数聚合用任意精度 int；浮点聚合用
  `math.fsum`（正确舍入、与顺序无关），任意次数运行逐位一致
  （`tests/test_scheduler.py::test_two_runs_bit_identical` 字节级比对）。
- **持久化**：节点状态、输出摘要（sha256）、阶段水位、内存峰值、逻辑时钟
  全部在 `BEGIN IMMEDIATE` 事务中原子写入；节点推进前重校验父节点摘要，
  不匹配拒绝推进（`E_DIGEST_MISMATCH`）。
- **恢复**：崩溃时处于 `RUNNING` 的节点视为半提交，恢复时重置为 `PENDING`
  重放；`COMMITTED` 节点绝不重算（attempt 计数保持 1）。结果文件与账本描述
  同一组已提交节点。
- **并发与幂等**：账本连接全程以可重入锁串行化；结果文件在
  `O_CREAT|O_EXCL` 文件锁下以临时文件 + `os.replace` 原子替换，读者只见完整
  已提交结果。同一节点同一摘要重复提交为幂等空操作；冲突摘要拒绝
  （`E_STATE_CONFLICT`）。
- **逻辑时钟**：每阶段按 `window_ms` 推进；推进前校验持久化时钟与期望基准
  偏差，超过 `tolerance_ms` 拒绝（`E_CLOCK_SKEW`）。

## 业务错误码

| 代码 | 含义 |
|---|---|
| `E_INPUT_MISSING` | 输入/管线文件缺失 |
| `E_BAD_JSON` | JSON 畸形（含行列号） |
| `E_BAD_PIPELINE` | 节点名重复、父节点缺失、阶段方向错误、成本/内存非正整数、可并行度超限等 |
| `E_CYCLE` | 依赖成环 |
| `E_BAD_BUDGET` | 阶段预算/时钟配置非法 |
| `E_DIGEST_MISMATCH` | 推进前输入摘要不匹配 |
| `E_CLOCK_SKEW` | 逻辑时钟回拨超过容差 |
| `E_NODE_FAILED` | 单节点计算异常（不污染其他节点） |
| `E_STATE_CONFLICT` | 冲突重复提交 / 文件锁超时 |

非法管线在解析阶段即抛出 `E_BAD_PIPELINE`，不会写入任何持久化状态
（`tests/test_validation.py::test_invalid_pipeline_writes_no_state`）。

## 验证结果（实测）

- `pytest tests/`：25 passed（校验、确定性、预算放弃、失败隔离、幂等提交、
  时钟回拨、摘要校验、强杀恢复、篡改检测），连续 6 轮全绿。
- 固定样本运行两次：`artifacts/run1.json` 与 `artifacts/run2.json` 字节一致。
- 预算放弃：`heavy_export`（200 MiB）超过阶段 2 预算（128 MiB）→ `ABANDONED`，
  原因落账，其余节点正常 `COMMITTED`。
- 强杀恢复：`--crash-after-node fast_sum` 在阶段 1 中途 `os._exit(3)`，
  账本中 `slow_sum` 为 `RUNNING`（半提交）；`resume` 仅重放 `slow_sum`
  （`requeued_after_crash: ["slow_sum"]`，`fast_sum.attempt` 保持 1），
  恢复结果与无中断运行字节一致，`verify` 全部检查通过。
