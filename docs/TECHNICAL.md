# Radapt：流式列式窗口聚合的确定性自适应重优化组件

`radapt` 是一个仅依赖 Python 标准库与 `numpy` 的离线运行时验证组件。它使用固定种子生成列式批次，逐批提交窗口聚合并观测代价漂移，在窗口边界裁决执行形态切换，通过定点误差账本记录精度风险，使用有界并行调度执行窗口任务，并将恢复状态事务化写入 SQLite。

## 快速运行

```bash
pip install -r requirements.txt
python scripts/generate_samples.py
python -m radapt.cli validate --sample samples/deterministic
python -m radapt.cli run \
  --sample samples/deterministic \
  --db artifacts/radapt.sqlite3 \
  --report artifacts/run-report.json
bash scripts/validate.sh
```

强制终止恢复演示：

```bash
python -m radapt.cli run --sample samples/deterministic \
  --db artifacts/kill.sqlite3 --report /tmp/kill.json \
  --crash-after-batch 2
# 进程以 137 / SIGKILL 退出

python -m radapt.cli recover --db artifacts/kill.sqlite3
python -m radapt.cli run --sample samples/deterministic \
  --db artifacts/kill.sqlite3 --report artifacts/recovered.json
```

## 模块结构

- `radapt/parsing.py`：四类输入的解析与全量校验，失败抛 `ValidationError(code, ...)`，不返回半成品 `Bundle`。
- `radapt/generator.py`：SHA-256 派生的确定性随机流、列式数组、倾斜键计数和批次摘要。
- `radapt/aggregate.py`：`hash` / `sort` 两种精确聚合、`Fraction` 算术、逐行真值通路和首个分歧定位。
- `radapt/fixedpoint.py`：定点换算、舍入方向、残差、加法/乘法误差上界和误差来源链。
- `radapt/costmodel.py`：偏差比、冷却裁决、候选成本估算、预算裁决和精度敏感标记。
- `radapt/scheduler.py`：并行上限、逻辑时间预算、内存预算和三类未完成结局。
- `radapt/storage.py`：SQLite schema、原子批次/水位提交、形态版本指针和拒绝回退。
- `radapt/engine.py`：逐批推进、提交后观测、边界切换、崩溃钩子和恢复重放。
- `radapt/sample.py` / `scripts/generate_samples.py`：固定种子样本。
- `tests/`：27 个校验、确定性、真值、调度、账本、预算、版本和恢复测试。

## 输入约定

四类 JSON 文件分别为 `table.json`、`plan.json`、`arrivals.json`、`config.json`。示例由生成脚本写入 `samples/deterministic/`。

关键配置字段：

- `drift_threshold`：`(0, 1)` 内的偏差阈值，支持小数字符串以避免二进制浮点歧义。
- `cooldown_ms`：非负逻辑冷却毫秒。
- `switch_cost_budget_ms`：非负切换预算。
- `max_parallel_tasks`、`logical_time_budget_per_round_ms`、`fixed_point_scale`：正整数。
- `memory_budget_bytes`：可选正整数，缺省 64 MiB。
- `cost_tolerance_ms`：候选代价近并列容差。

主要错误码包括：

| 场景 | 错误码 |
| --- | --- |
| 表名重复 | `E_TABLE_NAME_DUPLICATE` |
| 列类型非法 | `E_COLUMN_TYPE_INVALID` |
| 倾斜比例非法 | `E_SKEW_RATIO_INVALID` |
| 窗口参数非法 | `E_WINDOW_PARAM_INVALID` |
| 批次序号未严格递增 | `E_BATCH_SEQ_NOT_STRICTLY_INCREASING` |
| 逻辑时钟非法 | `E_LOGIC_CLOCK_INVALID` |
| 漂移阈值非法 | `E_DRIFT_THRESHOLD_INVALID` |
| 并行/时间/标度配置非法 | `E_PARALLEL_TASK_LIMIT_INVALID` / `E_LOGICAL_TIME_BUDGET_INVALID` / `E_FIXED_POINT_SCALE_INVALID` |
| 摘要不一致 | `E_BATCH_DIGEST_MISMATCH` |
| 聚合与真值不一致 | `E_AGGREGATE_VALUE_MISMATCH` |
| 切换预算不足 | `E_SWITCH_BUDGET_INSUFFICIENT` |
| 形态版本回退 | `E_FORM_VERSION_ROLLBACK` |
| 启动后任务失败 | `E_TASK_FAILED_AFTER_START` |

CLI 对输入错误输出 `{ok:false,error:{code,message,details}}`，校验类错误退出码为 2，业务裁决错误可由报告中的错误码判断。

## 确定性与摘要

随机数不使用系统墙钟或网络，而由 `GENERATOR_VERSION`、全局种子、表名、批次序号、列名和列类型经 SHA-256 计数器派生。倾斜列使用确定性上限取整：

```text
hot_key_count = min(row_count - 1, max(1, ceil(row_count * skew_ratio)))
```

每批摘要包含批次身份、行数、倾斜键实际比例以及每列的规范字节摘要。批次文件读取后会重新计算列摘要；引擎只接受与摘要匹配的列数据。重复生成的 `manifest.json` 逐字节一致。

## 真值与首个分歧

`ExactTruthPath` 对每个窗口、每个批次、每一行逐行执行精确聚合；`sum`、`avg` 和 decimal/double 归一化为 `Fraction`，因此不会受聚合顺序或浮点求和误差影响。

`find_first_divergence(...)` 按批次同时推进流式形态和逐行真值，在首个不一致批次返回：

- `first_divergent_batch`
- `summary_digest`
- 期望/实际分组值
- 双方行数与分组基数
- 已见批次区间和关键中间值

正常样本中 `hash`、`sort`、切换后形态以及 SQLite 恢复结果均与真值一致。

## 漂移与冷却

每个批次事务提交后，引擎只从 SQLite 中该窗口已提交批次构造观测：

```text
deviation_ratio(metric) = abs(observed - estimated) / estimated
```

观测指标为窗口行数、输出分组基数和确定性逻辑耗时。逻辑耗时来自实际行数、实际分组数和算子工作量公式；同时记录真实 `wall_elapsed` 仅供参考，裁决不使用墙钟，以保证可复现。

漂移事件包含表名、窗口 ID、最大偏差比、观测批次闭区间、各指标依据、阈值、剩余余量/超出量和冷却状态。冷却期内即使偏差仍超阈值，也不会插入第二条漂移事件。

## 切换与预算

切换只在窗口边界发生。进行中的窗口在 `window_state.form_version` 固定旧形态，任务启动时读取该版本；窗口结果与新形态指针由 `finalize_window_and_switch(...)` 在同一个 SQLite 事务中提交。

候选形态包含窗口顺序、每窗口算子和可选分组键顺序。切换成本由安装代价和确定性数据迁移代价组成，例如：

```text
row_migration_cost = observed_rows * (1/100 ms/row)
group_migration_cost = observed_cardinality * (1/10 ms/group)
```

每个候选记录评估值；被否决候选记录第一个否决原因，如 `E_SWITCH_BUDGET_INSUFFICIENT`、`HIGHER_TOTAL_COST` 或 `E_SWITCH_NO_ESTIMATED_IMPROVEMENT`。所有候选都超预算时保留原指针并返回 `E_SWITCH_BUDGET_INSUFFICIENT`。

## 定点与误差账本

`ErrorLedger` 对行数、基数、耗时、预算和切换成本按 `fixed_point_scale` 换算，记录：

- 标度和定点整数
- `floor_down`、`ceil_up` 或 `exact` 舍入方向
- 被舍弃/补入的残差
- 单次换算误差上界 `1/scale`
- 加法累计误差上界
- 乘法传播与舍入误差上界

当两个可行形态的精确代价差小于 `cost_tolerance_ms` 且选择依赖该排序时，报告标记 `precision_sensitive=true`，并持久化误差来源链：参与批次、换算 ID、乘法/加法步骤 ID 和总误差上界。样本中两个候选的精确差为 `1/10000000 ms`，在标度 `1000000` 下定点整数差为 0，因此会稳定触发精度敏感标记。

## 并行调度

`ParallelScheduler.run_round(...)` 接收按窗口分组的任务。启动前依次检查逻辑时间估计、内存估计和并行槽位：

- `completed_after_start`：启动并成功完成。
- `failed_after_start`：启动后抛错；调度器等待其他已启动任务，不取消运行中任务，引擎只在任务成功返回后提交。
- `not_started_within_budget_parallel_limit`：预算可容纳但并行槽位已满，留给下一波/下一轮。
- `abandoned_logical_budget` / `abandoned_memory_budget`：预算不足，主动放弃剩余窗口并记录原因。

线程池仅执行无数据库写入的内存聚合；SQLite 提交仍由编排线程完成，因此已启动任务不会产生“半任务提交”。

## SQLite 与强制终止恢复

数据库表包括：

- `batches`：批次身份、行数、摘要、摘要 JSON、实测指标。
- `window_state`：窗口状态、`open/finalized`、水位批次、固定形态版本、精确聚合状态和最终结果。
- `form_pointer` / `form_versions`：当前形态指针和单调版本历史。
- `drift_events` / `precision_flags`：漂移与精度敏感记录。
- `ledger_snapshot`：误差账本快照。
- `meta`：逻辑时钟水位和已应用漂移事件键。

批次摘要、指标、窗口水位、聚合状态和逻辑时钟在 `commit_batch(...)` 的同一事务中提交。恢复时：

1. 读取已提交 `seq` 和窗口水位。
2. 跳过所有已提交批次，不启动聚合任务。
3. 对 `open` 窗口从事务内状态继续；若末批次已提交但未完结，则只执行完结/边界切换。
4. 仅对缺失 `seq` 调用调度器重放。
5. 最终用逐行真值核对所有窗口。

`switch_form(...)` 和 `finalize_window_and_switch(...)` 都在 `BEGIN IMMEDIATE` 后检查当前版本；请求版本小于等于当前版本时返回 `E_FORM_VERSION_ROLLBACK`。

## 实际验证结果

下列结果由 `bash scripts/validate.sh` 在 WSL/Ubuntu 工作区实际执行，完整输出保存在 `artifacts/validation-results.txt`：

- Python：`Python 3.10.12`（代码使用 `from __future__ import annotations`，兼容 3.10；目标环境 3.11+ 可直接运行）。
- 测试：`27 passed`。
- 两次固定种子生成的 `manifest.json`：`diff` 无差异。
- `SIGKILL` 注入点：批次 2 事务提交后，进程退出码 `137`。
- 恢复跳过批次：`[1, 2]`。
- 恢复重放批次：`[3, 4, 5, 6]`。
- 连续执行与中断恢复的 `window_state.result_json`：完全一致。
- 三个窗口真值核对：全部 `matched=true`，行数分别为 `300`、`720`、`720`。
- 漂移事件：`orders/w1`，观测区间 `[1]`，最大偏差比 `1011/125 = 8.088`，阈值 `1/5 = 0.2`。
- 形态切换：`sort-baseline-v1` → `hash-compact-v3`；`w1` 保留版本 1，`w2/w3` 使用版本 3。
- 预算否决：`expensive-full-rehash-v9` 得到 `E_SWITCH_BUDGET_INSUFFICIENT`。
- 精度敏感：`hash-compact-v3` 与 `hash-balanced-v2` 的代价差为 `1/10000000 ms`，小于容差 `1/100 ms`，定点整数差为 0。

## 后续可选增强

- 将 JSON 输入 schema 发布为 JSON Schema 文件。
- 增加 Prometheus/OpenTelemetry 指标导出，但不把墙钟纳入确定性裁决。
- 支持更多聚合算子和两阶段分片合并；现有 `Fraction` 状态序列化可直接扩展。
