# GSB 归档保留水位裁决引擎 — 技术设计

数据库主从复制延迟诊断与归档保留水位裁决引擎。纯 Python 3.10+ 标准库实现
（`sqlite3` / `json` / `decimal` / `fcntl`），无第三方依赖、无网络、无真实数据库。

## 1. 目录与模块

```
engineering_delivery/seed_samples.py   # 随工程交付的固定确定性种子脚本（可复制、仅标准库）
seed_samples.py                        # prepare 时复制到工作区根的副本
samples/                               # 种子生成的 JSON / 分片 / v2 SQLite 元数据库
gsb/
  errors.py        # 业务错误码 E_BAD_STREAM / E_CONFLICT_POLICY / E_VERSION_ROLLBACK ...
  canon.py         # 规范 JSON、sha256 摘要、稳定输出
  fixedpoint.py    # 定点标度、舍入、逐实例余量结转、误差上界链
  clock.py         # FixedClock / SequenceClock 可注入逻辑时钟
  model.py         # 解析 + 强校验（流、段连续性、策略、检查点）
  streammerge.py   # 主/从位点流按逻辑时钟确定性归并
  merge.py         # v1/v2 策略跨版本合并与裁决依据
  delay.py         # 复制延迟、回放水位、未回放字节量
  judge.py         # 追赶余量、精度敏感、批次、段归档裁决（纯函数）
  patch.py         # 补丁闭包、快照校验/版本比较/节点差异
  store.py         # SQLite schema、事务、提交日志、幂等提交、沿用
  locking.py       # fcntl 跨进程文件锁
  engine.py        # load/merge/judge/resume/status/verify/patch/replace 编排
  cli.py           # argparse CLI；python -m gsb ...
tests/             # 42 个 unittest/pytest 用例（含崩溃与多进程）
scripts/           # prepare_samples.sh、run_acceptance.sh
```

## 2. 样本业务布局（固定种子）

逻辑时钟基准 `T0=1730000000000`，裁决时刻 `JUDGE_NOW_MS=1730000070350`
（模 1000 余 350，刻意制造秒级换算余量）。

- 14 个事务 `1001..1014`，6 个连续日志段，段区间连续且恰好覆盖全部事务。
- 11 个回放实例、19 条回放记录；`replay_id` 全局唯一，事务号在每个实例流内严格递增。
- `rep-gamma` 为墓碑实例；`rep-beta` 为高延迟实例。
- 两个精度敏感段对：`2|3`（提高标度后翻转）与 `4|5`（提高标度后不翻转）。
- 段 6 同时存在 `rep-iota` 的 `pin` 与 `rep-kappa` 的 `expedite`，产生 `E_CONFLICT_POLICY`。

预期裁决（默认参数）：段 1 `archivable`；段 2 因精度批次保守持有为 `retained`
（独立门限本已可归档，`precision_hold=true`）；段 3/4/5 `retained`；段 6 `alert`。

## 3. 校验与错误码（需求 1）

`gsb/model.py` 用 `IssueBag` 聚合字段级问题并映射错误码：

| 错误码 | 触发 |
| --- | --- |
| `E_BAD_STREAM` | 位点流缺字段/类型错（`bool` 不被当作 `int`）、事务号非严格递增、段号不连续、段区间不覆盖全部事务、回放标识/实例标识重复 |
| `E_BAD_POLICY` | 相对保留秒数不是非负整数、绝对毫秒戳不是整数、分片与 `meta.sqlite` 不一致、v2 内容摘要不符 |
| `E_CHECKPOINT_WATERMARK` | 节点引用的检查点不存在、依赖边水位回退 |
| `E_CONFLICT_POLICY` | 同段 `pin` 与 `expedite` 矛盾 |
| `E_VERSION_ROLLBACK` | 快照版本/代数低于活动修订 |
| `E_ALREADY_DECIDED` | 同段同修订已用不同摘要裁决 |
| `E_LOCK_BUSY` / `E_NOT_FOUND` / `E_STATE` | 锁超时 / 对象缺失 / 状态非法 |

CLI 业务错误输出 `{"ok":false,"error":{"code",...}}` 到 stderr，退出码 2（状态类 3）。

## 4. 两代策略合并（需求 2）

裁决全序（`gsb/merge.py`）：

1. `policy_clock_ms` 大者胜 — `basis=clock`（样本 `rep-alpha`）
2. 时钟相同，`generation` 大者胜 — `basis=generation`（`rep-beta`）
3. 都相同，来源格式优先级 `v2=2 > v1=1` — `basis=format_priority`（`rep-gamma`）

每个节点的 `sources[]` 保留两代的版本、快照号、时钟、代数、签名、原始时间表示。
v1 的 `now + relative_retention_seconds*1000` 经 `deadline_unit_ms=1000` 的
`ROUND_FLOOR` 换算，记录标度、舍入方向与 `discarded_remainder_ms`
（`rep-rho` 为 `350ms`）。

## 5. 延迟与定点余量（需求 3、4）

- 延迟差 = `replay_clock_ms - commit_clock_ms`，按实例用
  `delay_unit_ms=100`、`ROUND_FLOOR` 累计。
- `RemainderCarry` 每步把上一轮未满一个定点单位的余量并入新总额再取整，
  恒等式 `已确认单位*标度 + 结转余量 == 精确累计毫秒`，因此不重复计入。
  样本 `rep-alpha`：`43+91+120+67=321ms -> 3 单位，余 21ms`。
- 追赶余量按“该实例在该段内未回放字节”计算
  （`gsb/delay.py::segment_backlog_bytes`），流水线
  `ceil(bytes/rate) -> ceil(*safety) -> ceil(+slack)`，
  逐步登记除法/乘法/加法的局部与累计误差上界：
  `bound = unit*safety + 2*unit`。
- 精度敏感：相邻段门限差 `delta < batch_tolerance_ms=5`，或误差带可能跨越
  容忍度（`delta - err < tol`）且批次结论依赖该差值时标记。提高标度
  （单位 `/1000`）整条链重算，输出 `flip_if_scale_increased` 与
  `verdict_coarse/verdict_fine_scale`。样本 `2|3` 粗标度 `delta=4` 相连、
  细标度 `delta≈19.47` 分离 → 翻转；`4|5` 细标度 `delta≈3.03` 仍相连 → 不翻转。

## 6. 段裁决（需求 5）

每段取所有引用该段且非墓碑节点的：

- `watermark_deadline_ms = min(retention_deadline_ms)`（需求指定的最保守水位）
- `archive_gate_ms = min(deadline + catchup_margin)`

判定顺序：无活动引用 → `archivable`；矛盾指令 → `alert/E_CONFLICT_POLICY`；
引用实例延迟超阈值 → `alert/E_LAG_HIGH`；`gate <= injected_now` → `archivable`，
否则 `retained`。粗标度下 `delta < tol` 的相邻段并入同一归档批次，
批次门限取 `max`；若批次门限在未来而某段独立结论为可归档，则保守改判
`retained` 并置 `precision_hold`。

## 7. 补丁闭包与快照替换（需求 6）

- 补丁 `upsert_node/tombstone` 改动实例后，依赖边闭包
  （`gsb/patch.py::affected_closure`）= 节点治理的段 ∪ 这些段的相邻段
  （潜在新精度对）∪ 既有 `precision_batch` 边对端。样本 theta 补丁：
  重估 `[2,3,4]`，安全复用 `[1,5,6]`。
- 全量快照替换校验 `content_digest`、非空签名集合、语义化版本与代数；
  `(version_tuple, generation)` 低于活动修订即 `E_VERSION_ROLLBACK`。
  相同 `idempotency_key` 或相同 `content_digest` 重放返回原修订（只替换一次）。
- 未受影响段的决策、精度记录（两端都可复用）、冲突记录以 `carried=1`
  从事务内复制到新修订；之后 `judge` 只提交缺失（脏）段。

## 8. 持久化、崩溃恢复与并发幂等（需求 7、8）

SQLite（WAL、`synchronous=FULL`、`busy_timeout=30s`）表：
`meta / source_files / stream_watermarks / policy_revisions /
instance_deadlines / delay_evidence / decision_claims / segment_decisions /
precision_records / conflict_records / commit_journal / recovery_runs`。

崩溃协议：

1. `journal_start` 自动提交一条 `state='pending'`（持久的半提交标记）。
2. 决策行 + 精度 + 冲突 + journal→committed + `clock_watermark_ms`
   在同一个 `BEGIN IMMEDIATE` 事务提交；强杀时 SQLite 原子回滚，不存在半条决策行。
3. `resume`：有决策行=已提交；pending 且无决策行=半提交；无决策行=缺失。
   只对没有决策行的段重放；全部写入按主键/唯一键幂等，计数来自 `COUNT(*)`，
   因此重放不重复计入。merge 阶段的 pending 会先重放 merge。

并发：每段一个 `fcntl.flock` 锁文件 + `decision_claims` +
`segment_decisions PRIMARY KEY(rev_no,segment_id)` 三层保证。
提交时若已存在相同摘要决策 → 返回 `idempotent`；不同摘要 → `E_ALREADY_DECIDED`。
8 个 `judge --all` 并发的实测结果：6 行决策、0 重复、总提交 6、幂等重放 42。

## 9. CLI

```bash
bash scripts/prepare_samples.sh        # 复制种子脚本并生成 samples/
python3 -m gsb --samples samples --state state load
python3 -m gsb --samples samples --state state merge
python3 -m gsb --samples samples --state state judge          # 或 --segment 2 / --parallel 4
python3 -m gsb --samples samples --state state resume
python3 -m gsb --samples samples --state state status         # sort_keys 稳定 JSON
python3 -m gsb --samples samples --state state verify
python3 -m gsb --samples samples --state state patch   --file samples/patches/p1_theta.json
python3 -m gsb --samples samples --state state replace --file samples/snapshots/snapshot_v3_good.json
```

崩溃注入：`judge --crash-after 2`（提交 2 段后，在下一段 pending 时 `os._exit(137)`）、
`merge --crash-phase merge`、`judge --crash-phase delay`、`judge --slow-ms 400`
（供真实 `kill -9` 测试）。

## 10. 确定性保证

- 不读墙钟：裁决时刻只来自 `--now` 或种子阈值；提交时间戳使用注入逻辑时钟。
- 所有集合输出前排序，JSON 用 `sort_keys=True`，Decimal 以字符串保精度。
- 相同样本 + 相同时钟下，决策摘要、延迟值、status 字节级可重复
  （测试与验收中用双跑摘要比对证明）。
