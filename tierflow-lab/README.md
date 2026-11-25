# TierFlow 冷热分层迁移裁决组件

一个完全本地、确定性的冷热分层迁移裁决实现：固定种子生成样本，SQLite 保存权威状态，文件锁与事务保证多进程和崩溃恢复一致性。

## 目录

- `scripts/generate_samples.py`：固定种子样本与 v2 SQLite 元数据库生成器。
- `scripts/acceptance.sh`：一键运行样本生成、11 个测试、CLI 验收和崩溃恢复验收。
- `src/tierflow/validation.py`：对象、配置、事件解析校验。
- `src/tierflow/policy.py`：v1/v2 策略读取、时间换算、墓碑与跨版本裁决。
- `src/tierflow/heat.py`：可注入逻辑时钟下的确定性热度评分。
- `src/tierflow/ledger.py`：定点代价、舍入余量和误差来源链。
- `src/tierflow/storage.py`：SQLite schema、文件锁、策略快照指针。
- `src/tierflow/scheduler.py`：预算/容量/并发调度、原子提交、恢复重放。
- `src/tierflow/diagnostics.py`：`verify` 一致性检查。
- `tests/test_tierflow.py`：单元、CLI、SIGKILL 恢复和三进程并发测试。
- `samples/`：生成的确定性 JSON、blob、v2 分片与策略元数据库。
- `validation/`：最近一次验收命令的实际 JSON 输出和摘要。

## 快速开始

```bash
cd tierflow-lab
python3 -m pip install -r requirements.txt
python3 scripts/generate_samples.py
export PYTHONPATH=src

python3 -m tierflow.cli load --reset
python3 -m tierflow.cli merge
python3 -m tierflow.cli judge
python3 -m tierflow.cli migrate --round-id round-0001
python3 -m tierflow.cli status --round-id round-0001
python3 -m tierflow.cli verify
```

也可以安装控制台入口后直接使用 `tierflow`：

```bash
python3 -m pip install -e . --no-deps
tierflow verify
```

完整验收：

```bash
bash scripts/acceptance.sh
```

## CLI 子命令

全局参数 `--samples`、`--db`、`--clock` 必须放在子命令前。

- `load [--reset]`：校验摘要、字节数、阈值、事件序号和引用，并初始化 SQLite。
- `merge [--update-package PATH]`：合并 v1/v2，执行策略快照替换、追加、noop 或回退拒绝。
- `judge [--clock MS]`：输出热度分量、目标层和时钟检查。
- `migrate --round-id ID`：调度并并发执行迁移；支持 `--workers`、`--budget-bytes`、`--fail-object`。
- `resume [--round-id ID] [--force] [--retry-failed]`：识别死进程、租约过期、`claimed/running/committing` 任务并重放。
- `status [--round-id ID]`：输出策略指针、容量账目、水位和任务状态。
- `verify`：校验输入文件、SQLite integrity、外键、容量余额、唯一迁移、水位和代价账本。

## 关键业务规则

### 热度与时钟

热度满分 `10000bp`，按固定顺序组合：

1. 最近访问逻辑时钟对应的近因分；
2. 访问计数对应的频次分；
3. 声明热度权重分（样本中 `heat_weight` 为 `0..100` 百分点）。

中间阈值区间使用迟滞：`score >= hot_threshold` 到热层，`score <= cold_threshold` 到冷层，中间区间保持当前层。相同数据库和相同 `--clock` 输出排序键固定的 JSON，可逐位复现。

时钟水位持久化在 `clock_watermark`。若注入时钟比已提交水位低超过 `clock_rollback_tolerance_ms`，返回：

- 字符串码：`E_CLOCK_ROLLBACK`
- 数字码：`2001`

### 策略合并

同一策略键的裁决顺序为：

1. 更高声明逻辑时钟获胜；
2. 逻辑时钟相同则来源格式优先级 `v2 > v1`；
3. 同来源同钟再比较策略代数。

每条合并结果保留：

- 获胜来源格式和来源包版本；
- 原始时间表示；
- v1 相对秒换算到注入时钟绝对毫秒的公式、舍入方向、舍入余量和补整余量；
- 候选版本与裁决依据链。

策略包通过临时表 `policy_snapshot_stage` 装载完整快照，再原子写入版本表并切换 `meta.policy_current_version`。重复包为 `noop`，安全新增为 `incremental_append`，不兼容变化为 `full_replace`。清单版本、策略代数或代数下限回退返回：

- `E_POLICY_ROLLBACK` / `3001`

### 定点代价

默认标度为 `10^6`。代价链依次记录：

- 字节数到传输毫秒的定点换算；
- 复杂度乘法与向下舍入；
- 固定开销加法。

每一步保存原始精确有理值、舍入方向、舍入余数、当前累计值和累计误差上界。两个候选代价差小于 `cost_model.difference_tolerance_scaled` 且调度名次依赖该差值时，输出 `precision_sensitive=true` 及双方误差来源链。

种子中的 `obj-0006` 与 `obj-0007` 仅相差 1 字节，定点代价差为 `256`，容差为 `1000`，因此稳定触发精度敏感标记。

### 调度与三类结局

调度名次为 `urgency DESC, precision-aware cost order, object_key ASC`。计划阶段和认领事务都会检查：

- 轮次字节预算；
- 目标层 `used + reserved + size <= capacity`；
- 全局活跃任务数不超过配置并发上限。

结局分类：

- `within_budget_unstarted`：已通过预算/容量投影但本轮尚未启动；
- `started_failed`：已启动并失败，记录退避截止时钟；
- `voluntarily_abandoned`：预算或容量不足，主动放弃边界候选及后续候选。

种子轮次 `round-0001`/验收轮 `acceptance` 的确定结果是 5 个成功、3 个主动放弃。使用 `--workers 1 --max-launch 1` 可额外看到 4 个预算内未启动候选。

### 原子性、恢复和并发

认领时在 `BEGIN IMMEDIATE` 事务中：

- 通过 `(round_id, policy_version, object_key)` 唯一键防止重复迁移；
- 增加轮次已认领字节；
- 增加目标层预留容量；
- 写入容量账本和 owner/租约。

成功提交在单个 SQLite 事务中完成：

- 任务 CAS 到 `succeeded`；
- 对象层级从源层 CAS 到目标层；
- 源层扣减 `used_bytes`；
- 目标层增加 `used_bytes` 并释放等额 `reserved_bytes`；
- 迁移水位 `migration_committed + 1`；
- 更新代价账本时间戳；
- 推进逻辑时钟水位。

因此不存在“部分容量已扣、对象层未更新”的已提交事务。`claimed`、`running`、`committing` 都是可识别的中断状态；恢复器依据 PID 存活性和租约判断死任务，沿用原预留重放幂等提交。重复 resume 不会再次增加水位或余额。

多进程运行时，`fcntl` 文件锁保护调度临界区，SQLite 唯一约束和条件更新是最终防线。测试启动 3 个 CLI 进程并行处理同一轮，确认无重复对象、无容量超扣、水位恰为 5。

## 实际验证

最近一次实际运行结果见 `VALIDATION.md` 和 `validation/summary.txt`。自动化测试包含真实 `SIGKILL` 子进程和三进程并发，而不是只用内存模拟。
