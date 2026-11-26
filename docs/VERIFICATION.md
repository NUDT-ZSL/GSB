# 实际验证记录

验证环境：WSL Ubuntu、Python 3.10.12、numpy 2.0.0、SQLite 使用标准库。
固定种子 `20261127`，默认生成 48 行原料。

## 1. 固定种子生成

```bash
python3 generate_data.py --out data --force
```

关键校验和：

| 产物 | SHA-256 |
|---|---|
| `raw_samples.jsonl` | `sha256:02628dc48c6893af3e7e879fff75ad5934dd6c965fd8915a8ebe8ebcce0628d4` |
| `templates.json` | `sha256:c5c02074af5adc5fed51f68e2179ed46e469d9740b42fa8e13b3e4a29d72b3db` |
| `cleaning_rules.json` | `sha256:2cc732992a7566215d6fe27bc611326d93ec815a1c013206f76a5802b661949c` |
| `quota_targets.json` | `sha256:20d03dab1f533aa330be6653306058859464cd400d064c5ff345643ff9cb49bc` |
| v1 策略 | `sha256:cd342c47c28fd0aef9997ce2aaf123ccd52c15d6e997319340efa31bac091135` |
| v2 策略 | `sha256:a4ed2c1ceb46c448d7c4f72047a270c062ecea7e72298bcb77425968e26af49b` |

完整清单：`artifacts/generation.json`。

## 2. 六阶段完整运行

```bash
python3 -m ifdata run \
  --data data \
  --state artifacts/verification/state \
  --out artifacts/verification/export
```

结果：

| 指标 | 实际值 |
|---|---|
| 状态 | `complete` |
| 快照版本 | `1,2,3,4,5,6` 连续 |
| 摄入 | 48 行有效 |
| 去重输入 | 48 |
| 去重簇 | 20 |
| 去重保留 / 剔除 | 20 / 28 |
| 候选 / 实际比较 | 230 / 230 |
| 每样本唯一成簇 | `true` |
| 配比总量 | 18 / 18 |
| 导出行数 | 18 |
| 数据集校验和 | `sha256:1bf02ce73d8171357b49622788909f700e98650bc63b26b4e4e747a19dddb708` |

`qa:gold` 目标声明 4 条，但总量在第 18 条到达，因此实际 3 条，记录
`TOTAL_CAP_REACHED`。这验证了“优先总量并允许受限欠配”。

证据：`artifacts/verification/run-v2.json`、`status.json`、`export/`。

## 3. v1/v2 一致性

同一输入分别使用 v1 单文件和 v2 分片 + SQLite 运行：

```text
v1 dataset sha256 == v2 dataset sha256 ==
sha256:1bf02ce73d8171357b49622788909f700e98650bc63b26b4e4e747a19dddb708
```

两份 `dataset.jsonl` 字节完全一致。导出 manifest 的策略校验和不同，符合
两代快照元数据差异。

证据：`run-v1.json`、`export-v1/dataset.jsonl`。

## 4. 比较预算与候选窗口裁决

```bash
python3 -m ifdata run ... --compare-budget 200
```

- 窗口 16 估算 230 次比较，超出预算 30，返回记录
  `E_COMPARE_BUDGET`，裁决为 rejected；
- 回退窗口 8，实际比较 176 次，裁决为 accepted；
- 阶段完成，低预算裁决和预算值均写入 dedup 快照。

证据：`run-low-budget.json`、
`state-low-budget/stages/04-dedup.json`。

## 5. 内存预算与 worker 降级

```bash
python3 -m ifdata run ... --memory-limit 600000
```

- 3 worker 估算 983040 字节，超过 600000，拒绝；
- 2 worker 估算 720896 字节，超过 600000，拒绝；
- 选择 1 worker，估算 458752 字节；
- dedup 实际峰值批次内存 393216 字节。

证据：`run-low-memory.json`、
`state-low-memory/stages/04-dedup.json`。

`budget-check` 对 worker 5、比较 200、内存 5 MB 的单批次返回主错误
`E_CONCURRENCY_LIMIT`，同时保留三项越界明细。

证据：`budget-check.json`。

## 6. 配比档位裁决

对本地去重结果施加 `wiki:gold` 配额 6：

- 优先级 `sampling_target > dedup_quality`：选中 6，其中 2 条由 silver
  档放宽，记录 accepted；
- 优先级 `dedup_quality > sampling_target`：选中 4，放宽候选记录
  rejected，错误码 `E_QUALITY_POLICY`。

证据：`sampling-relaxation.json`。

## 7. 强制终止与恢复

```bash
python3 -m ifdata run ... --fail-stage quality
python3 -m ifdata resume ...
```

第一次运行在 quality 计算后、提交前终止，退出码 130，返回：

```json
{
  "code": "E_FORCED_TERMINATION",
  "stage": "quality",
  "committed_state_preserved": true
}
```

恢复结果：

- `resumed_from_stage = template`
- `started_at_stage = quality`
- 新提交阶段为 `quality, dedup, sampling, export`
- 事件流中 ingest/template 各只出现一次 `stage_started`
- quality 首次为 `stage_terminated_before_commit`，第二次才 `stage_committed`
- 重新启动时放置的 `.tmp-orphan` 被清理，且没有重算已提交阶段

证据：`resume-termination.json`、`resume-after.json`、
`resume-cleanup.json`、`state-resume/events.jsonl`。

## 8. worker 异常归因

```bash
python3 -m ifdata run ... --failpoint quality:1=forced-worker-error
```

返回：

```json
{
  "code": "E_WORKER_FAILURE",
  "stage": "quality",
  "batch_id": "quality-batch-0001"
}
```

ingest 和 template 快照仍保留，可在修复后恢复。

证据：`worker-failure.json`、`state-worker-fail/`。

## 9. 策略迁移

v1 -> v2：

- `strategy_generation = 2`
- 1 个 v1 `retired_keys` 项转为墓碑
- 时间从 ISO 转为绝对 Unix 毫秒
- 记录 `completed_by` 和 `completed_fields`

v2 -> v1：

- 1 个墓碑无法表达，未写入 v1
- 墓碑全文写入审计文件
- v1 `migration_note` 记录
  `V1_CANNOT_REPRESENT_TOMBSTONES`

证据：`migrate-v1-to-v2.json`、`migrate-v2-to-v1.json`、
`migrated-v1-discard-audit.json`。

## 10. 策略并发提交

两个并发 v2 提交中：

- 先到者提交版本 3 成功；
- 后到者立即返回 `E_STAGE_BUSY`；
- 活跃版本仍为 3；
- SQLite 中只有版本 3 成为活跃快照。

证据：`strategy-concurrency.json`。

## 11. 自动化测试

```bash
python3 -m unittest discover -s tests -v
```

结果：

```text
Ran 27 tests in 1.978s
OK
```

完整输出：`artifacts/verification/unittest.txt`。

