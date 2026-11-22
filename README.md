# 本地流式连接运行时自适应重优化

这是一个从零实现、可离线复现的本地流式连接执行与运行时重优化组件。它不使用 Docker，不访问外部数据源，批次完全由本地样本、全局种子和计划到达顺序确定性生成。

核心能力：

- 严格解析表描述、连接计划、统计快照和重优化配置。
- 对列式批次做逐位可复现生成，并记录每批摘要与行数。
- 按连接顺序和 `hash` / `sort_merge` / `nested_loop` 算法执行前缀连接。
- 只用已提交批次的完整结果生成观测，不使用后续批次回填。
- 根据 NDV、连接选择率、倾斜和代价偏差判定漂移，支持逻辑时钟冷却。
- 在批次边界评估新连接顺序和算法，执行切换代价预算裁决。
- 记录误差上界、误差来源链和精度敏感标记。
- 使用 SQLite + 文件锁实现原子提交、幂等、并发一致性与崩溃恢复。
- 提供 `run`、`observe`、`reopt`、`resume`、`status`、`verify` 子命令。

## 快速开始

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

.venv/bin/python scripts/verify_environment.py
.venv/bin/python scripts/generate_batches.py \
  --sample samples/reopt_demo.json \
  --summary-only \
  --out artifacts/batch_manifest.json

# 如需把每批列式数组落成 .npy
.venv/bin/python scripts/generate_batches.py \
  --sample samples/reopt_demo.json \
  --arrays-dir artifacts/batches

.venv/bin/python -m stream_reopt run \
  --sample samples/reopt_demo.json \
  --db artifacts/runtime.db

.venv/bin/python -m stream_reopt status \
  --sample samples/reopt_demo.json \
  --db artifacts/runtime.db

.venv/bin/python -m stream_reopt verify \
  --sample samples/reopt_demo.json \
  --db artifacts/runtime.db
```

`status --db ...` 不要求样本参数，也可以只读已持久化状态。输出使用 `json.dumps(sort_keys=True)`，列表按批次、事件序号稳定排序。

常用命令：

```bash
# 最多推进 4 批
.venv/bin/python -m stream_reopt run --sample samples/reopt_demo.json --db artifacts/runtime.db --batches 4

# 4 个进程并发推进同一计划
.venv/bin/python -m stream_reopt run --sample samples/reopt_demo.json --db artifacts/parallel.db --workers 4

# 查看最近 3 个观测
.venv/bin/python -m stream_reopt observe --sample samples/reopt_demo.json --db artifacts/runtime.db --last 3

# 在当前批次边界强制评估一次切换
.venv/bin/python -m stream_reopt reopt --sample samples/reopt_demo.json --db artifacts/runtime.db --force

# 事务写入后、COMMIT 前 SIGKILL，用于复验恢复
.venv/bin/python -m stream_reopt run \
  --sample samples/reopt_demo.json \
  --db artifacts/crash.db \
  --crash-before-commit-arrival 5

# 强制终止后恢复
.venv/bin/python -m stream_reopt resume \
  --sample samples/reopt_demo.json \
  --db artifacts/crash.db
```

`E_SWITCH_BUDGET` 的 CLI 退出码为 `3`；非法样本退出码为 `2`；校验失败为 `4`；状态不存在或不匹配为 `5`。

## 样本

主样本位于 `samples/reopt_demo.json`，包含：

- 3 张表：`customers`、`orders`、`lineitems`
- 9 个交错到达的确定性批次
- 初始计划：`["lineitems", "orders", "customers"]`，算法 `["nested_loop", "hash"]`
- 陈旧统计快照：NDV 与连接选择率显著偏离真实生成分布
- 倾斜声明：`orders.customer_id` 中热点键占 35%
- 重优化配置：漂移阈值、120ms 冷却逻辑时钟、切换预算、容差和误差预算

其他样本：

- `samples/budget_exhausted.json`：切换候选存在但预算不足，保留原形态并返回 `E_SWITCH_BUDGET`。
- `samples/precision_sensitive.json`：两个候选估计代价差小于容差，产生精度敏感标记和误差来源链。
- `samples/invalid/*.json`：非法表、列宽、NDV、批次、类型、统计快照和配置。
- `samples/anomaly_manifest.json`：异常注入清单及期望业务错误码。

样本 JSON 的主要结构：

```json
{
  "sample_id": "id",
  "global_seed": 20261123,
  "tables": [{"name": "t", "row_count": 100, "columns": [], "skew": {}}],
  "plan": {
    "query_id": "q",
    "initial_shape": {"join_order": ["a", "b"], "algorithms": ["hash"]},
    "joins": [],
    "batches": [{"arrival": 1, "table": "a", "row_count": 50}]
  },
  "statistics": {"tables": {}, "join_selectivities": []},
  "reopt": {
    "drift_threshold": 0.25,
    "cooldown_ms": 120,
    "clock_step_ms": 50,
    "switch_cost_budget": 20.0,
    "float_tolerance": 1e-9,
    "error_bound_budget": 1e-3
  }
}
```

## 实现结构

- `stream_reopt/sample.py`：JSON 解析与业务校验，在打开数据库前拒绝非法输入。
- `stream_reopt/generator.py`：稳定种子、PCG64 批次生成、规范化列摘要。
- `stream_reopt/join.py`：三种连接算法、规范化行序、结果摘要、连接次序连通性检查。
- `stream_reopt/observation.py`：前缀观测、NDV/连接选择率偏差、倾斜偏差和误差链。
- `stream_reopt/cost.py`：候选形态枚举、代价模型、切换代价、预算和精度敏感裁决。
- `stream_reopt/storage.py`：SQLite 模式、文件锁、读写事务、状态查询。
- `stream_reopt/engine.py`：批次提交、观测、冷却、切换、恢复和真值验证。
- `stream_reopt/cli.py`：六个子命令和稳定 JSON 输出。
- `tests/`：21 项确定性、并发、恢复和端到端测试。
- `docs/TECHNICAL.md`：公式、事务序列、误差传播和恢复设计。

详细算法与技术论证见 `docs/TECHNICAL.md`。

## 正确性要点

每个批次提交是单个 `BEGIN IMMEDIATE` 事务，事务内同时写入：

1. 已提交批次摘要和行数；
2. 观测记录；
3. 漂移累计和触发记录；
4. 重优化记录；
5. 当前形态版本、结果摘要和批次水位。

同一批次的观测、切换和状态指针不会分属不同事务。形态切换只在批次完整提交后发生，因此进行中的批次始终使用旧形态完成。切换后使用上一版本已提交的全部数据重新构造规范化连接结果，并与切换前摘要比较。

恢复时重新生成已提交批次的列数据，并逐批校验数据库中保存的摘要。未提交事务由 SQLite 自动回滚，所以只会重放未提交批次。`verify` 再对每个历史观测前缀按当时形态版本重放，并把当前形态结果与初始形态一次性全量结果逐行比较。

## 实际验证

完整机器可读报告：`artifacts/validation_report.json`。固定批次清单：`artifacts/batch_manifest.json`。

| 项目 | 结果 |
| --- | --- |
| 环境 | Python 3.11.18、numpy 2.4.6、pytest 9.2.0、SQLite 3.53.1 |
| 单元/集成测试 | 21 项通过 |
| 批次确定性 | 9 批摘要重复运行完全一致，`manifest_sha256=1842f4a51eaedf57e2a13ec4e9175fa5fe1fea72d40b661789e84a62bf91f9f5` |
| 漂移触发 | 第 3 批逻辑时钟 150ms 触发，来源含表名、连接键、偏差比和批次区间 |
| 边界切换 | 版本 `1 -> 2 -> 3`，切换分别发生在第 3、9 批提交后 |
| 冷却累计 | 第 2、4、5、7、8 批漂移超阈值但仅累计，记录原因 `cooldown` |
| 全量真值 | 33,498 行，初始形态与最终形态 `row_by_row_match=true` |
| 预算不足 | 第 3 批返回 `E_SWITCH_BUDGET`，形态版本保持 `1` |
| 精度敏感 | 标记为真，候选代价差 `0.0 < 250.0`，记录两个候选误差上界与来源链 |
| SIGKILL 恢复 | 强杀前水位 4/版本 2；恢复仅推进第 5–9 批，最终版本 3 |
| 崩溃后校验 | 9 批摘要、9 个观测重放均通过，逐行真值一致 |
| 4 进程并发 | 9 个批次均只提交一次，读取结果完整，`verify=true` |
| 幂等提交 | 重复提交同一批次返回 `idempotent=true`，观测数仍为 1 |
| 状态输出 | 两次 `status --db` 输出逐字节一致 |

复跑全部实验：

```bash
.venv/bin/python scripts/run_experiments.py
```

该脚本重新生成实验数据库、执行主流程/预算/精度/强杀恢复/并发/幂等实验、运行 pytest，并覆盖 `artifacts/validation_report.json`。

## 约束

- 当前连接图按等值内连接处理，不支持外连接、非等值谓词或自连接。
- 结果使用规范化排序后的行序列作为比较口径；重复行会保留。
- 连接结果在小规模样本上每次从已提交前缀完整重算，以保证观测语义简单、可审计；生产规模可替换为增量算子而不改变事务和裁决接口。
- 逻辑冷却不依赖墙钟，保证相同输入和相同提交顺序下输出逐位稳定。
