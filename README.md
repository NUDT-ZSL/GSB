# Balance Lab：确定性战斗数值平衡验证

一个不依赖网络、外部服务或容器的本地验证工程。它用固定种子生成战斗脚本、数值表、关卡判据、预算和编辑脚本，把战斗按回合切成 SQLite 账本中的分块，并在数值编辑后通过依赖闭包只重算失效部分。

## 快速开始

要求 Python 3.11 或 3.12（代码也兼容已验证的 Python 3.10）。

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Linux/macOS：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

生成样本并验证：

```bash
python -m balance_lab.cli generate-samples --output samples
python -m balance_lab.cli validate --input samples
python -m balance_lab.cli run --input samples --state-dir state --reports-dir reports --verify
python -m balance_lab.cli evolve --input samples --work-dir build/edits --state-dir state-evolve --reports-dir reports-evolve
python -m pytest -q
```

主要输出：

- `samples/*.json`：固定种子 `20261125` 生成的五类输入。
- `state/balance.db`：分块 SQLite 账本。
- `reports/balance_report.json`：平衡诊断、调度准入、残差和一致性报告。
- `reports-evolve/invalidation_report.json`：每一步编辑的失效键、理由和分块数。
- `reports-evolve/balance_report.step-*.json`：每步编辑后的报告快照。

## 命令与退出码

- `generate-samples`：确定性重生成全部 JSON。
- `validate`：只解析校验，不创建状态目录或账本。
- `run --verify`：调度缺失分块、聚合平衡结论，并与全量推演比对。
- `evolve`：顺序应用 `edit_script.json`，逐步做依赖失效、增量重算和全量一致性验证。
- `verify-consistency`：打开现有账本并与当前输入的从头全量推演比对。

退出码：

- `0`：成功。
- `2`：输入业务校验失败，JSON 中含 `E_*` 错误码。
- `3`：增量结果与全量结果不一致。
- `130`：收到可处理终止信号，已在分块边界停止并写出 `interrupt.json`。
- `1`：预算、账本、报告版本冲突或其他运行错误。

## 核心设计

**定点与浮点双轨**

- 标度固定为 `1_000_000`，输入属性、倍率、概率和权重先用 `Decimal` 精确量化为整数。
- 伤害、治疗、暴击、收益系数和掉落价值均以 Python 整数完成定点累加/比较，NumPy 提供 `int64` 求和与 `float64` 残差统计。
- 每个回合同步记录 float64 参考值、绝对/相对差值和首个超差的回合、单位、技能。
- 平衡判定只读取定点 raw 值；浮点只进入诊断报告。

**确定性分块**

- 每场战斗按 `chunk_rounds=2` 切分，样本共 `3 + 4 + 6 = 13` 块。
- 回合内动作键为 `(logic_time, declaration_index)`；相同时刻稳定保持 JSON 声明顺序。
- 暴击和掉落使用 `SHA-256` 派生的稳定整数随机量，不使用线程调度相关的全局 RNG。
- 分块边界状态只包含生命、能量、存活和状态效果；奖励等聚合量由各块指标求和，因此纯掉落/判据编辑不会污染战斗状态边界。

**依赖图与失效闭包**

每个分块持久化四类精确依赖：

- `unit:<id>:<attr>`
- `skill:<id>:<param>`
- `drop_table:<id>:weight:<channel>`
- `criterion:<id>:<field>`

编辑命中键后，依赖图找出直接引用分块；若编辑可能改变战斗状态，则沿同一战斗的分块状态链传播到后续块。掉落和判据类编辑只失效直接引用块。报告逐块给出 `direct_dependency` 或 `state_boundary` 理由。

固定样本的四步编辑分别失效 `7、10、4、6` 块，且每步增量结果均与全量重算一致。

**SQLite 阶段完成**

`chunks` 表在单事务中写入战斗、块序号、回合区间、状态摘要、内容摘要和完整可恢复 blob。只有某战斗的全部预期块均存在且摘要有效时，才执行聚合、平衡判定并写入 `battle_results`。重启时会重新计算 SHA-256 摘要，摘要无效的行先清理再重算。

**双预算调度**

- `parallelism` 对应有界 `ThreadPoolExecutor(max_workers=...)`。
- 每个在算块按单位数、回合数和动作数预估状态缓冲；同一并行批次的预留总和不得超过 `memory_budget_bytes`，单块超预算返回 `E_MEMORY_CHUNK_EXCEEDS_BUDGET`。
- 可注入 `ManualLogicalClock` / `WallLogicalClock` 计量调度轮；逻辑服务量超过 `logic_time_budget_ms` 后结束当前轮并让出，剩余块进入后续轮次。
- 待算块按输入战斗顺序和块序号排序，相同输入、相同预算下准入序列可复现。

**中断与原子报告**

- `SIGINT`、`SIGTERM`，以及 Windows 的 `SIGBREAK` 只设置事件；调度器在当前批次/分块边界后停止，写 `state/interrupt.json`，以 `130` 退出。
- 强杀不会产生半提交块：SQLite 事务保证块行完整，重启只承认摘要有效的已提交块。
- 报告先写到同目录唯一 `.tmp` 文件，`flush + fsync` 后通过 `os.replace` 整体替换；替换前校验 `report_format` 和可选的 `version_id` CAS。

## 验证

实际验证记录见 `docs/VERIFICATION.md`。自动化测试包含非法输入、定点残差、顺序键、平衡失败判据、并行确定性、编辑闭包、摘要损坏、预算、报告 CAS、SIGTERM 恢复和 POSIX `SIGKILL` 恢复。
