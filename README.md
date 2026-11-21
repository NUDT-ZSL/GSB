# TierLab

本地、确定性、可中断恢复的冷热分层迁移裁决与诊断工作台。实现只依赖
Python 3.11+、NumPy 和标准库 `sqlite3`，不访问网络，不读取系统墙钟，
所有时间判定都来自注入的逻辑时钟。

## 快速开始

```bash
# 当前工作区已准备 .venv（Python 3.11.18 + NumPy 2.4.6）。
# 如需重建：
python3.11 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/generate_samples.py --out samples

.venv/bin/python -m tierlab load \
  --db /tmp/tierlab.db \
  --manifest samples/small/manifest.json \
  --config samples/small/config.json \
  --clock-table samples/small/clocks.json --clock-step 1

.venv/bin/python -m tierlab migrate \
  --db /tmp/tierlab.db --bucket small-a --round 1 \
  --clock-table samples/small/clocks.json --clock-step 2

.venv/bin/python -m tierlab status --db /tmp/tierlab.db
.venv/bin/python -m tierlab verify --db /tmp/tierlab.db
```

子命令：

- `load`：完整校验清单/配置/逻辑时钟后，初始化一个桶；任何校验错误都在
  打开数据库前返回，因此不会创建数据库或锁文件。
- `migrate`：规划并执行一轮迁移。支持 `--mode auto|exact|sampled`、
  `--fail-key`、`--fail-count`、`--fail-every` 进行确定性失败注入。
- `resume`：终结崩溃后遗留的在途批次，只处理未完成项，并重算账目。
- `status`：输出稳定排序 JSON，包括每个对象的层级/目标层级/热度分数、
  两层容量账目、在途登记、退避时间、最近模式裁决和估算详情。
- `verify`：校验对象层归集与层账目、容量/预留、在途状态、批次完成状态、
  轮次水位和逻辑时钟水位。失败时退出码为 `3`。

业务错误统一输出 `{"ok": false, "error": {"code": "...", ...}}`，退出码
为 `2`。错误码定义在 `tierlab/errors.py`。

## 输入格式

对象清单：

```json
{
  "bucket": "small-a",
  "objects": [
    {
      "key": "small/object-000",
      "size_bytes": 120,
      "last_access_clock": 980,
      "access_count": 0,
      "heat_weight": 1,
      "declared_tier": "hot",
      "migratable": true
    }
  ]
}
```

分层配置：

```json
{
  "hot_capacity_bytes": 5376,
  "cold_capacity_bytes": 9912,
  "round_byte_budget": 1000,
  "migration_concurrency": 2,
  "hot_threshold": "5",
  "clock_tolerance": 100,
  "mode": "auto",
  "backoff": {
    "base_delay_clocks": 50,
    "max_delay_clocks": 200,
    "multiplier": 2,
    "max_consecutive_failures": 3
  },
  "sampling": {
    "ratio": "0.25",
    "confidence_z": "1.96",
    "min_sample_per_stratum": 2,
    "random_seed": 7
  },
  "adaptive": {
    "exact_max_objects": 200,
    "exact_max_candidates": 200,
    "max_estimated_error_ratio": "0.35",
    "exact_scan_unit_cost": "1",
    "exact_candidate_unit_cost": "0.05",
    "sampled_index_unit_cost": "0.1",
    "sampled_object_unit_cost": "0.2"
  }
}
```

逻辑时钟表可以是 `{"steps": [{"step": 1, "clock": 1000}, ...]}`，也可以是
整数数组。`--clock-step` 按 `step` 选择；也可以直接传 `--clock`。

## 热度与层级

评分使用固定 Decimal 上下文（28 位精度）和固定运算顺序：

```text
age = max(0, logical_clock - last_access_clock)
recency = 1 / (1 + age)
score = round_12((heat_weight * recency) * (1 + access_count))
target_layer = "hot" if score >= hot_threshold else "cold"
```

同一输入、同一逻辑时钟和同一配置总是产生逐位一致的分数与目标层级。
逻辑时钟低于持久化水位但不超过容差时，采用持久化水位继续判定；超过
容差返回 `E_CLOCK_ROLLBACK`，事务不提交，层级视图保持不变。

## 精确与采样模式

- `exact`：逐对象计算，候选数、候选字节和对象观测均来自全量真值。
- `sampled`：按当前层分层，用固定 seed 确定性抽取样本；按层外推候选数
  与候选字节，输出置信误差界和最坏情况误差界、样本键、样本观测及外推
  真值来源。调度只启动抽样中实际观察到的候选对象。
- `auto`：先做确定性采样，使用对象总数、估计候选数、估计误差比例和
  两种模式成本进行裁决。超过规模阈值且误差/成本可接受时切换采样；
  否则保留精确扫描。批次结果和 `status` 会输出实际模式、触发条件、
  被否决模式、否决原因和成本估计。

采样误差界基于无放回有限总体方差与配置的 `confidence_z`；同时给出
未观测对象全部为候选时的最坏情况界。`mode` 切换只影响本轮候选生成，
不修改任何已提交账目。

## 调度与状态机

候选按“离阈值越远越优先、同分按键排序”启动。每轮依次检查：

1. 剩余字节预算；
2. 目标层剩余容量（已用 + 在途预留）；
3. 迁移并发上限。

预算耗尽后，剩余候选全部记录为 `abandoned_budget`；目标层容量不足时，
该目标层后续候选记录为 `abandoned_capacity`；并发已满但预算/容量仍允许
的候选记录为 `not_started_within_budget` 并保留到下一轮。启动项失败记录
为 `started_failed`，达到连续失败上限后记录为 `abandoned_retry_limit`。

对象状态为 `pending -> migrating -> migrated | failed | abandoned`。
失败退避：

```text
delay = min(base_delay_clocks * multiplier^(failures - 1), max_delay_clocks)
backoff_until_clock = batch_effective_clock + delay
```

退避截止只与注入逻辑时钟有关。成功迁移在单个 SQLite 事务内更新对象
层级、版本、失败计数、层账目、在途状态、轮次/时钟水位和批次结果。

## 持久化、恢复与并发

核心表为 `objects`、`layers`、`batches`、`batch_items`、`inflight`。
每个写入命令使用 `flock` 文件锁串行化工作台写入，再通过
`BEGIN IMMEDIATE` 事务操作 SQLite WAL。唯一约束保证同一对象同一轮次
同一版本最多一次在途/提交；批次主键和请求摘要保证相同批次重复提交幂等。

迁移分两阶段：

1. 规划事务写入批次、候选审计、目标层预留和 `migrating` 状态；
2. 终结事务统一写入成功/失败结果、退避、对象层级、层账目、在途状态、
   轮次水位和逻辑时钟水位。

可注入的强制终止点：

- `TIERLAB_CRASH_AT=during_reserve`：预留事务中途退出；
- `TIERLAB_CRASH_AT=after_reserve`：预留已提交、终结前退出；
- `TIERLAB_CRASH_AT=during_finalize`：终结事务中途退出。

`resume` 读取持久化批次和 `planned_outcome`，跳过已完成项，识别已到目标
层的已提交项，只终结未完成项，然后按对象层归集重算两层账目。恢复后
`hot.used + cold.used == SUM(object.size_bytes)`。

## 确定性样本与验证

```bash
.venv/bin/python scripts/generate_samples.py --out samples
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/run_demo.py --output artifacts/validation_report.json
```

样本：

- `samples/small`：24 对象，精确模式；单轮同时覆盖成功、预算放弃、
  容量放弃和预算内未启动。
- `samples/adaptive`：1200 对象，自动模式切换到分层采样。

实际验证记录写入 `artifacts/validation_report.json` 与
`artifacts/test_results.txt`。当前环境实测：

- Python `3.11.18`、NumPy `2.4.6`、SQLite `3.53.1`；
- 单元/集成测试 `16/16` 通过；
- 小样本精确轮次候选 `12`，启动 `2`，四类结局齐全；
- 大样本自动选择 `sampled`，样本 `180/1200`，估计候选 `607`，
  候选数置信误差界 `93`；精确真值为候选 `593`，落在估计区间 `[514, 700]`；
- 三种强制终止点均可恢复，恢复后 `verify` 通过；
- 不读取 `time`/`datetime`/单调时钟，所有命令只使用注入逻辑时钟。

## 目录

```text
tierlab/
  parsing.py       输入校验
  scoring.py       固定精度热度
  candidates.py    精确/分层采样/自适应裁决
  service.py       调度、状态机、恢复、status/verify
  storage.py       SQLite schema、事务、文件锁
  cli.py           五个子命令
scripts/
  generate_samples.py
  run_demo.py
tests/
samples/
```
