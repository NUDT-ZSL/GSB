# 本地事务日志归档与保留策略裁决引擎

这是一个不依赖容器、外部数据库服务或系统墙钟的本地保留策略裁决引擎。它读取主库事务位点流、多个从库回放位点流、v1 相对保留策略和 v2 分片策略，合并出统一的“日志段 -> 回放实例 -> 策略”依赖视图，并为每个日志段输出可归档、需保留或需告警裁决及完整证据链。

实现使用 Python 3.12、SQLite WAL 和 NumPy；数据目录、SQLite 账本和输出均为确定性结果。

## 快速开始

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e '.[test]'

.venv/bin/python -m log_retention.sample_data --root sample
./retention --config sample/config.json load
./retention --config sample/config.json merge
./retention --config sample/config.json judge
./retention --config sample/config.json rebuild
./retention --config sample/config.json verify
./retention --config sample/config.json status
```

也可以直接运行：

```bash
./scripts/run_demo.sh
./scripts/crash_recovery_demo.sh
./scripts/verify_demo.sh
```

`retention` 会优先使用 `.venv/bin/python`，否则回退到 `python3`。根命令参数必须放在子命令之前，例如：

```bash
./retention --config sample/config.json --clock-ms 21000 judge
```

## 样本布局

```text
sample/
  config.json
  primary/transactions.jsonl
  replica/replay.jsonl
  policies/v1/retention.jsonl
  policies/v2/policy_meta.db
  policies/v2/shards/part-00.json
  policies/v2/shards/part-01.json
  sample_summary.json
  state/ledger.db            # 运行后生成，不提交
```

样本包含 5 个连续日志段、15 个主库事务、4 个回放实例和 10 个策略节点：

- `replica-a`：v1/v2 同签名重叠，v2 来源优先并保留 v1 原始相对秒数证据。
- `replica-b`：段 3 上存在两个同实例策略分叉；`v2-b-seg3-new` 代数更高，胜出；`v2-b-seg3-old` 在 rebuild 时作废；最近公共祖先为 `v2-b-root`。
- `replica-c`：v1 策略被 v2 后代覆盖。
- `replica-d`：回放结果已作废，策略墓碑为真，不进入有效保留视图。

`sample/state/ledger.db` 由 `load` 创建，样本生成脚本不会提交运行态账本。

## 数据与校验

主库记录字段为 `txn_id`、`commit_ms`、`byte_size`、`segment_id`。回放记录字段为 `txn_id`、`instance_id`、`replay_ms`、`result`、`tombstone`。

加载前会在内存中完成全部校验，非法输入不会创建或修改 SQLite 文件。主要规则：

- 主库事务号严格递增；每个回放实例内的事务号严格递增。
- 日志段号连续，每个段至少有一个事务，且所有主库事务都有合法段。
- 回放事务必须存在于主库；`result` 只能是 `active` 或 `invalidated`。
- 一旦实例回放结果为 `invalidated`，该实例只能有墓碑策略；反之不能出现墓碑策略。
- v1 相对保留秒数为非负整数；v2 绝对截止毫秒为整数；最小追赶余量为非负整数。
- 策略父节点必须存在、属于同一实例，且沿谱系保留代数单调不降。
- 策略、分片、SQLite 元数据之间重叠字段必须一致。

稳定业务错误码包括：

| 错误码 | 含义 |
| --- | --- |
| `E_PRIMARY_TXN_ORDER` | 主库事务号乱序 |
| `E_PRIMARY_SEGMENT_GAP` | 日志段不连续或缺失 |
| `E_REPLAY_TXN_ORDER` | 同一实例回放事务号乱序 |
| `E_REPLAY_UNKNOWN_TXN` | 回放引用不存在的事务 |
| `E_REPLAY_TOMBSTONE_MISMATCH` | 回放结果与墓碑布尔值矛盾 |
| `E_POLICY_RELATIVE_SECONDS` | 相对保留秒数非法 |
| `E_POLICY_GENERATION_ORDER` | 谱系保留代数回退 |
| `E_POLICY_TOMBSTONE_REPLAY_MISMATCH` | 策略墓碑与最终回放结果矛盾 |
| `E_POLICY_V2_SOURCE_MISMATCH` | v2 分片与 SQLite 元数据不一致 |
| `E_CLOCK_ROLLBACK` | 注入逻辑时钟回拨超过容差 |
| `E_LOAD_VERSION_REGRESSION` | 输入装载版本回退 |
| `E_OPERATION_INCOMPLETE` | 两阶段操作仍有未完成节点 |
| `E_VERIFY_MISMATCH` | 账本与全量重算不一致 |

## 策略合并语义

同签名定义为 `(instance_id, segment_id, retention_generation, effective_at_ms)`。同签名同时出现在 v1 和 v2 时：

1. 逻辑时钟相同时按 `source_priority` 选择来源格式，默认 `v2=20 > v1=10`。
2. 未选中的节点保留在视图和策略历史中，但 `selected=0`。
3. 选中节点保存 `source_version`、原始时间表示、原始相对秒数、原始绝对毫秒值和合并候选清单。

每个实例的策略谱系是一棵有向树。某日志段的有效候选是“覆盖该段且没有覆盖该段的非作废后代”的叶节点。这样全局父策略仍可覆盖未被分叉子策略覆盖的段，而分叉子策略只覆盖自己的段。

同一段、同一实例出现多个有效候选且截止时刻或墓碑状态矛盾时，按以下稳定顺序仲裁：

```text
retention_generation DESC,
effective_at_ms DESC,
source_priority DESC,
policy_key DESC
```

judge 阶段会选择唯一裁决策略，但同时输出 `ALERT` 和待作废证据；rebuild 后才真正写墓碑并从有效视图移除。

## 定点换算

v1 换算：

```text
absolute_cutoff_ms = injected_clock_ms - relative_retention_seconds * 1000
```

该换算标度固定为每秒 1000 毫秒，舍入方向为 `exact`，余量为 0，并随证据落账。

v2 绝对毫秒戳原值保留。

追赶余量按实例计算：

```text
numerator = unreplayed_bytes * 1000 * fixed_scale_units_per_ms + carry_in_units
computed_units, remainder_units = floor_divmod(numerator, replay_rate_bytes_per_sec)
effective_units = max(computed_units, declared_min_units)
```

未满一个定点单位的 `remainder_units` 按实例持久化，只在新的逻辑时钟窗口作为 `carry_in_units` 累加。同一实例、同一保留代数、同一逻辑时钟和同一输入的窗口有唯一约束；重复 judge 复用原窗口，不会重复累加余量。

每个日志段的保守水位为：

```text
per_instance_adjusted = raw_cutoff_ms - floor(effective_margin_units / fixed_scale_units_per_ms)
conservative_retention_watermark_ms = min(per_instance_adjusted)
```

输出同时保留 `raw_min_retention_cutoff_ms`，因此可以区分原始策略截止时刻和计入追赶余量后的有效保留水位。`fixed_margin_units_total` 只对引用该段的唯一回放实例求和，避免同一实例被多个策略节点重复计数。

## 裁决规则

令 `M` 为段内最大提交逻辑时钟：

- `M <= 保守水位` 且 `M <= 归档目标`：`ARCHIVE`
- `M <= 归档目标` 但 `M > 保守水位`：`RETAIN`
- `M <= 保守水位` 但 `M > 归档目标`：`RETAIN`
- 两者都不满足：`RETAIN`
- 无有效保留引用、存在未重建冲突，或定点余量敏感且距边界不超过 1ms：`ALERT`

judge 输出按段号稳定排序，每段包含：

- 裁决、原因码、事务区间和提交时钟区间。
- 原始最小截止时刻、计入余量后的保守水位、定点余量总和。
- `precision_sensitive` 标记和边界判定。
- `segment:<id> -> instance:<id> -> policy:<key>` 路径证据。
- 被否决策略的拒绝路径、冲突排名与公共祖先。

## 作废与重建

rebuild 对每个未决冲突：

1. 将失败策略及其后代整体标记为作废，写入 `policy_invalidations` 和 `policy_history` 墓碑记录。
2. 记录获胜策略、公共祖先、冲突号和仲裁依据。
3. 从策略快照中过滤所有被作废节点，独立构建“干净视图”并逐段计算裁决哈希。
4. 只有干净视图与重建后视图逐段一致，且干净视图无冲突，才提交新视图。

失败策略从有效依赖视图中消失，但仍可从 `policies`、`policy_history` 和 `policy_invalidations` 追溯。

## SQLite 账本与恢复

账本开启 WAL、`synchronous=FULL` 和 30 秒 busy timeout。关键表包括：

- `load_versions` / `load_pointer`：输入版本及整体切换指针。
- `staging_primary` / `staging_replay` / `staging_policies`：装载临时表。
- `view_versions` / `view_pointer`：完整保留视图及切换指针。
- `view_policies` / `view_edges` / `view_instance_state`：策略、依赖边和实例余量。
- `segment_decisions`：逐段裁决与证据哈希。
- `conflict_records` / `policy_invalidations` / `policy_history` / `rebuild_records`：冲突与墓碑历史。
- `operations` / `op_calculations` / `retention_windows` / `rebuild_stages`：两阶段操作和恢复状态。

输入整体替换先写 staging 表，再在同一个 SQLite 事务内复制到正式表、切换 `load_pointer` 并删除旧 `view_pointer`。读取方只能看到指针指向的完整视图。

judge 和 rebuild 分两阶段：

1. 每个实例或作废节点单独提交 staging 行，进程可在此阶段被强制终止。
2. 所有节点齐备后，在全局文件锁和 `BEGIN IMMEDIATE` 中一次性写视图、决策、墓碑并切换指针。

恢复时只补缺失的 staging 行。余量窗口有唯一键，已提交窗口直接复用，所以恢复不会重复累加追赶余量。最终事务若被终止，SQLite 会自动回滚整个视图切换。

## 并发协议

- 每个实例使用独立文件锁，不同实例的余量计算可并行。
- SQLite `BEGIN IMMEDIATE` 保证同一实例、代数、时钟、输入只生成一个窗口。
- 最终视图提交使用全局账本锁，`view_versions.fingerprint` 唯一约束保证只整体替换一次。
- 同一 judge 操作的 `(kind, fingerprint, load_version)` 唯一；已提交结果从 `operation_results` 复用。
- 同一段余量只按唯一实例聚合，不因策略路径重复累加。

并行示例：

```bash
./retention --config sample/config.json judge --instance replica-a &
./retention --config sample/config.json judge --instance replica-b &
./retention --config sample/config.json judge --instance replica-c &
wait
```

最后一个完成的 worker 会在所有实例已 staging 后提交一次完整视图；较早完成的 worker 返回 `staged`。

## 命令说明

| 命令 | 作用 |
| --- | --- |
| `load` | 校验并原子装载流和两代策略 |
| `merge` | 跨版本合并并建立段到实例的依赖视图 |
| `judge` | 计算定点余量、裁决每段并输出路径证据 |
| `judge --instance <id>` | 单实例 staging，用于并行 worker |
| `judge --stage-only` | 只 staging，不提交视图 |
| `rebuild` | 墓碑作废冲突策略并从公共祖先重建 |
| `resume` | 恢复 staging/finalizing 的 judge 或 rebuild 操作 |
| `status` | 输出装载版本、视图版本、有效/作废策略和时钟水位 |
| `verify` | 从当前视图全量重算策略前沿、依赖边、余量与段裁决并比对账本 |

## 故障注入

测试和演示脚本通过环境变量在已提交 staging 后主动中断进程：

```text
RETENTION_FAULT_JUDGE_AFTER_STAGED=<n>
RETENTION_FAULT_REBUILD_AFTER_STAGED=<n>
RETENTION_FAULT_AFTER_FINALIZING=<n>
```

中断返回退出码 99，已提交 staging 保留，旧视图仍可读；随后运行 `resume` 完成剩余工作。

## 实际验证结果

环境：

```text
Python 3.12.13
SQLite 3.53.1
NumPy 2.5.5
pytest 9.2.0
```

自动化测试：

```text
$ .venv/bin/python -m pytest -q
..........................                                               [100%]
26 passed
```

样本正常路径：

```text
load:    15 transactions, 22 replay events, 10 policies
merge:   view=1, conflicts=1, content_hash=a4c4ffd7487fea74...
judge:   view=2, content_hash=0c1e954acb7e5d68...
rebuild: view=3, invalidated=v2:v2-b-seg3-old,
         common_ancestor=v2:v2-b-root, clean_equivalence=true
verify:  view=3, 5 segments verified,
         content_hash=dc7e75760e39b25f...
```

judge 逐段结果：

| 段 | 裁决 | 原因 |
| --- | --- | --- |
| 1 | `ARCHIVE` | 安全且达到归档目标 |
| 2 | `ALERT` | 定点余数敏感且位于决策边界 |
| 3 | `ALERT` | 存在待 rebuild 的策略冲突 |
| 4 | `RETAIN` | 保守保留水位与归档目标均未满足 |
| 5 | `RETAIN` | 保守保留水位与归档目标均未满足 |

重建后段 3 变为 `RETAIN`，原因码 `E_RETENTION_BLOCKS_TARGET`，未决冲突为 0。

故障恢复实际结果：

```text
judge 在 1 个实例 staging 后中断 -> resume 补 2 个实例 -> view=2 -> verify ok
rebuild 在 1 个策略 staging 后中断 -> resume 完成墓碑与视图 -> view=3 -> verify ok
```

并发测试同时启动三个独立进程分别处理 `replica-a/b/c`，最终只有一次 committed judge 视图；重复 judge 返回同一 `view_version` 与逐段结果，`verify` 通过。

所有时间判断只读取配置、输入或账本中的逻辑时钟；代码不使用 `datetime.now`、`time.time` 或系统本地时间。
