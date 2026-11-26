# PEFT Snapshot Diagnostics

本地、确定性、可交互的 PEFT 训练诊断应用。覆盖任务摄入、v1/v2 快照互操作、
语义迁移、梯度累加训练、阶段检查点、强制中断恢复、定向缓存失效、追溯重放与
结构化业务错误。全部样本由固定种子生成器在本地产生，不使用真实模型、网络、
Docker 或系统时间参与训练状态更新。

## 快速开始

环境要求：Linux/WSL Ubuntu、Python 3.10+、NumPy、Python 内置 SQLite3。

```bash
python3 -m peft_diag.cli generate --output data/generated --seed 20261127
python3 -m peft_diag.cli validate \
  --tasks data/generated/tasks.jsonl \
  --catalog data/generated/catalog.json --all-formats
python3 -m peft_diag.cli train \
  --tasks data/generated/tasks.jsonl \
  --catalog data/generated/catalog.json \
  --state-dir data/state --run-id demo --snapshot-format v2
python3 -m peft_diag.cli dashboard --state-dir data/state --run-id demo
```

运行自动化测试和完整验收：

```bash
python3 -m unittest -v tests.test_peft_diag
python3 scripts/verify_all.py
```

验收报告写入 `artifacts/verification_report.json`，可重复生成。上次实际执行结果
为 `ok: true`，14 项检查全部通过，任务清单 SHA-256 为
`7daa812c9b3cc74bf3dd3c7c1a3f1f4cdecffc84b805e8f45f5528c39019d566`。

## 工程结构

```text
peft_diag/
  canonical.py    规范化 JSON、SHA-256、原子写入、首个语义差异
  tasks.py        任务 JSONL 解析、结构/范围校验、规范化字典
  snapshots.py    v1/v2 读取写入、哈希校验、代数墓碑、双向迁移
  training.py     确定性微步梯度累加与 Adam 状态推进
  cache.py        内容寻址阶段缓存与确定性 LRU 降级
  checkpoint.py   不可变提交文件链与原子 latest 指针
  engine.py       四阶段编排、恢复、缓存复用、资源策略
  revision.py     修订事务锁、定向失效计划、双格式快照更新
  replay.py       版本过期判定与无缓存独立重放
  generator.py    固定种子任务、v1/v2 样本、增量样本、故障样本
  cli.py          命令行与结构化业务错误输出
  diagnostics.py  状态机、缓存、失效范围、恢复说明仪表盘
tests/test_peft_diag.py
scripts/verify_all.py
```

## 任务与确定性

每个 JSONL 行包含 `case_id`、`base_model_rev`、`adapter_type`、`adapter_dim`、
`num_layers`、`lr`、`steps`、`micro_batch`、`seed`、`revision`、
`logical_clock_ms`。支持 `task_version=v1`；未知版本返回 `E_UNSUPPORTED`。

校验规则：

- `case_id` 唯一且匹配受限字符集；重复返回 `E_FORMAT/case_id`。
- `base_model_rev` 只支持 `r1/r2`；`adapter_type` 只支持 `lora/qlora`。
- `num_layers` 必须等于基础模型层数，`adapter_dim` 必须为偶数且在范围内。
- 适配器结构逐层展开为 `layer.<idx>.<module>.<A|B>`，可训练参数总数由结构计算，
  并可与可选的 `expected_trainable_params` 交叉校验。
- `lr` 必须位于 `(0, 1]`；`steps` 必须为正且是 `micro_batch` 的整数倍。
- `seed` 位于 `[0, 2^32-1]`；`revision` 与逻辑时钟必须非负。
- 任务按 `(case_id, logical_clock_ms)` 升序规范化。同一输入、种子和配置输出相同
  规范化 JSON 字节、结构摘要和任务指纹。

`steps` 是微步总数；每累计 `micro_batch` 个微步执行一次 Adam 更新。因此
每个任务的优化器更新次数为 `steps // micro_batch`。

## 快照格式

### v1

单个 JSON 文件，含版本头、任务/修订元数据、显式 `time_origin_ms` 与
`effective_seconds`，以及扁平 `layers` 映射。每层记录 `layer_idx`、`shape`、
`values`、`momentum`、`variance` 和 `seed`。绝对毫秒按
`time_origin_ms + round(effective_seconds * 1000)` 精确换算。

### v2

目录快照：`index.json` 是版本头，`metadata.sqlite` 保存绝对毫秒、快照代数、
修订号、每分片路径/代数/形状/内容哈希/墓碑标记，`shards/*.json` 保存参数和优化器
状态分片。

读取规则：

- 先读版本头，再分派 v1 或 v2 路径。
- v2 对每个层只选择最高代数的有效分片；最高代数墓碑会明确拒绝该层，不回退旧代。
- 所有非墓碑分片都校验原始文件字节 SHA-256 与 SQLite 记录一致。
- 任一缺失、哈希不符或结构不一致返回 `E_SNAPSHOT_CORRUPT`，并指出层键与字段。
- 固定种子生成器额外产生带 gen-1 修订和旧分片墓碑的增量样本，用于验证上述规则。

双向迁移：

- v1 -> v2 补全绝对毫秒、代数默认 0、墓碑默认空、逐分片内容哈希和分片边界，
  并逐项记录补全来源。
- v2 -> v1 先解析当前有效分片，再展平层映射；显式丢弃代数、墓碑、增量历史与
  分片边界。报告记录丢弃规则。
- 迁移后重新加载，比较规范化内部语义字节；不等时返回 `E_SEMANTIC_DRIFT`，
  报告首个差异的层键和字段。

## 训练与恢复

训练状态完全由已提交快照、任务字段和显式算法常量决定。微梯度使用任务种子、
层键、微步序号和当前参数的确定性哈希/三角公式，不调用随机数或系统时钟。

四阶段状态机：

```text
INITIALIZE -> GRAD_ACCUM -> OPTIMIZER_UPDATE -> EXPORT
```

`GRAD_ACCUM` 对全部任务完成一个梯度窗口后才提交；`OPTIMIZER_UPDATE` 对全部任务
完成该窗口更新后才提交；`EXPORT` 全部完成后再提交。每个提交包含单调递增版本号、
全部任务状态、已完成任务集合、配置/任务/快照指纹和内容校验和。提交文件不可变，
`latest.json` 原子替换。

恢复规则：

- 正常 SIGINT/SIGTERM 在当前阶段处理完并提交，然后在提交边界停止并写报告。
- 强制终止只会留下最近提交。恢复从该阶段之后继续，已提交阶段不重算。
- 未提交阶段的内存工作丢弃，从阶段起点重建。
- 阶段缓存条目只有在覆盖它的检查点提交成功后才发布，因此崩溃前未提交的逐任务
  缓存不会被后续恢复误用。

## 修订失效传播

修订提交使用 `flock` 非阻塞独占锁。并发后到者立即返回 `E_STAGE_BUSY`。任务
版本回退返回 `E_FORMAT/revision`。修订成功后任务清单与 v1/v2 快照目录一起更新，
配置代数递增并写入修订历史和失效报告。

缓存键是内容寻址的，并区分阶段前缀：

- 快照语义哈希、修订、适配器结构、种子进入 `INIT`。
- `GRAD:1` 依赖 `INIT` 和微批配置，不依赖学习率。
- `UPDATE:1` 依赖梯度键和学习率。
- 后续梯度键依赖上一优化器更新键，因此学习率只使 `OPTIMIZER_UPDATE` 起及其后缀失效。

未被影响的任务与前缀继续命中持久缓存；受影响任务只重算前缀之后的阶段。失效
报告列出受影响任务、字段、阶段和逐键前后值。重算结果与干净全量训练逐字节比较。

示例：仅调整学习率并立即增量训练，配置代数会递增，`INIT/GRAD:1` 仍命中，
`OPTIMIZER_UPDATE` 及其后缀重算。

```bash
python3 -m peft_diag.cli revise \
  --tasks data/generated/tasks.jsonl \
  --catalog data/generated/catalog.json \
  --state-dir data/state \
  --case-id case-003 --lr 0.002 \
  --run --new-run-id lr-revision
```

结构化修订需要 `--revision 1 --snapshot <new-v2-dir>`；服务会校验新快照并同时
发布 v1/v2 等价目录，适配器维度变化必须提供匹配结构的新快照。

## 追溯重放

每个任务产物记录：

- 快照 ID、格式、路径、语义哈希、代数与修订；
- 配置代数、参数修订；
- 全部关键阶段缓存键；
- 步数、微批量、种子、学习率与优化器步数；
- 最终参数摘要、最终优化器摘要、最终状态摘要和每微步/优化步记录。

重放先比较当前任务字段、快照语义哈希、修订和全部阶段缓存键。任一依赖变化即
返回 `E_STALE_RESULT`，并给任务与首个过期字段及完整变化字段列表。判定通过后，
才能从当前已核对快照全量独立重算；重放不使用缓存，也不允许用旧快照修补当前依赖。

## 资源上限与降级

CLI 可配置：

```bash
--max-task-memory-bytes 67108864
--max-cache-entries 256
--max-open-shards 8
--max-recompute-tasks 32
```

显式降级优先级：

1. 单任务内存超限：阶段状态落内容缓存/检查点并记录
   `spill_stage_state_to_content_cache_and_checkpoint`。
2. 缓存条目超限：确定性 LRU 驱逐并记录被驱逐任务与键。
3. 分片数超过同时打开上限：顺序分批读取并记录。
4. 重算任务超过上限：按 `case_id` 顺序分批重算并记录。

这些事件进入运行报告的 `degradation` 列表，仪表盘会展示优先级与动作。

## 交互与诊断

`dashboard` 展示：

- 当前检查点版本/阶段/校验和；
- 阶段状态机与恢复规则；
- 各任务微步与优化器步进度；
- 每任务各阶段缓存命中/计算次数；
- 最近修订失效范围；
- 检查点恢复说明、业务错误码与资源降级。

加 `--interactive` 可使用 `status/tasks/cache/revisions/recover/help/quit` 命令。

## 实际验收

`python3 scripts/verify_all.py` 上次执行结果：

| 检查 | 结果 |
|---|---|
| 固定种子任务规范化 | 通过 |
| v1/v2 语义等价 | 通过，6/6 任务 |
| v2 增量代数与墓碑 | 通过 |
| v1<->v2 双向迁移 | 通过 |
| 完整训练 v1/v2 最终状态等价 | 通过 |
| 子进程强制终止（退出码 137）后恢复 | 通过，未提交阶段缓存命中为 0 |
| SIGTERM 提交边界优雅停止 | 通过，报告已写 |
| 学习率修订定向后缀失效 | 通过，`INIT/GRAD:1` 命中、更新后缀重算 |
| 适配器维度修订与双格式快照 | 通过，四阶段全失效 |
| 分片快照单独替换 | 通过，全部阶段缓存键变化 |
| 修订并发锁 | 通过，`E_STAGE_BUSY` |
| 追溯独立重放 | 通过 |
| 旧产物过期拒绝 | 通过，`E_STALE_RESULT/case-003` |
| 分片损坏拒绝 | 通过，`E_SNAPSHOT_CORRUPT` |

`python3 -m unittest -v tests.test_peft_diag` 覆盖任务错误码、快照损坏、迁移漂移、
微步续跑、强制中断恢复、修订缓存后缀、跨进程锁、重放过期和资源降级。

