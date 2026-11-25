# GSB 诊断工作台技术说明

## 1. 交付结构

- `workbench/canonical.py`：规范 JSON、SHA-256、观测摘要。
- `workbench/ioformats.py`：注册清单、JSONL、分片清单、补丁解析与业务校验。
- `workbench/rules.py`：v1/v2 规则解析、时间换算、跨版本裁决、依赖闭包和补丁推进。
- `workbench/engine.py`：确定性排序、补洞、健康状态机、代际失效和账本比对。
- `workbench/storage.py`：SQLite schema、文件锁、幂等唯一约束、临时表 stage、版本指针和崩溃点。
- `workbench/pipeline.py`：`load`、`merge`、`reconcile`、`patch`、`resume`、`status` 的事务编排。
- `workbench/verification.py` / `workbench/cli.py`：`verify` 与七个必需子命令。
- `workbench/seed.py`：固定种子样本生成器。
- `workbench/acceptance.py`：六种分片顺序、缺失分片、崩溃恢复和并发验收证明。
- `tests/test_workbench.py`：11 个单元/集成测试。
- `scripts/acceptance.sh`：一键复现实验。

## 2. 快速复现

```bash
pip install -r requirements.txt
python scripts/generate_sample.py --out samples
bash scripts/acceptance.sh
```

手动管线：

```bash
DB=artifacts/demo.db
CLOCK=1760000000000

python -m workbench load --db "$DB" \
  --registry samples/registry.json --clock-ms "$CLOCK"

python -m workbench merge --db "$DB" \
  --v1 samples/rules_v1.json \
  --v2-manifest samples/rules_v2_manifest.json \
  --clock-ms "$CLOCK" \
  --registry-for-diff samples/registry.json \
  --observations-for-diff samples/observations.jsonl \
  --out artifacts/merge-decision.json

python -m workbench load --db "$DB" \
  --observations samples/observations.jsonl

python -m workbench reconcile --db "$DB" --clock-ms "$CLOCK"
python -m workbench status --db "$DB" --out artifacts/status.json
```

分片乱序：

```bash
for shard in obs-shard-003 obs-shard-002 obs-shard-001; do
  python -m workbench load --db "$DB" \
    --shard-manifest samples/observation_shards_manifest.json \
    --only-shard "$shard"
  python -m workbench reconcile --db "$DB" --clock-ms "$CLOCK"
done
python -m workbench verify --expected artifacts/baseline-status.json \
  --actual-db "$DB"
```

崩溃自验证：

```bash
python -m workbench verify --self-test
```

Web 工作台：

```bash
python -m workbench serve --db artifacts/demo.db --port 5050
```

## 3. 确定性账本

所有跨进程比较均使用规范 JSON：

```text
json.dumps(value, ensure_ascii=False, sort_keys=True,
           separators=(",", ":"), allow_nan=False)
```

观测摘要只覆盖五个业务内容字段：
`instance_id`、`seq`、`logical_clock_ms`、`status_code`、`latency_ms`。

健康结论账本只包含业务结论，不包含会随加载方式变化的来源文件名。
`source_shard`、`loaded_shards` 等运维字段保留在 SQLite 的完整
`ledger_entries.entry_json` 中，但不进入 `ledger_versions.canonical_json`
的健康结论哈希。绝对路径放在规则视图的 `source_locations`，不参与
`rule_view.content_hash`，因此复制样本到其他临时目录后哈希仍稳定。

## 4. 校验与错误码

常见业务错误码：

- `E_REGISTRY_INVALID` / `E_DUPLICATE_SERVICE` / `E_DUPLICATE_INSTANCE`
- `E_ENDPOINT_NOT_LOOPBACK` / `E_STATUS_CODES_INVALID`
- `E_OBSERVATION_DIGEST_MISMATCH`
- `E_OBSERVATION_SEQUENCE_ORDER` / `E_OBSERVATION_SEQUENCE_DUPLICATE`
- `E_RULE_V1_INVALID` / `E_RULE_V1_MAPPING_INVALID`
- `E_RULE_V2_INVALID` / `E_RULE_V2_DB_INVALID`
- `E_SHARD_DIGEST_MISMATCH` / `E_SHARD_COVERAGE_MISMATCH`
- `E_RULE_GENERATION_NONMONOTONIC` / `E_RULE_GENERATION_CONFLICT`
- `E_PATCH_INVALID` / `E_PATCH_ID_CONFLICT`
- `E_LEASE_GENERATION_REGRESSED`
- `E_RESULT_MISMATCH`

错误输出形态：

```json
{"ok": false, "error": {"code": "E_RESULT_MISMATCH", "message": "...", "details": {"first_instance_id": "inst-alpha", "first_observation_seq": 3}}}
```

## 5. 规则合并与时间换算

v1 的非负整数 `effective_after_seconds` 在注入时钟 `C` 下换算为：

```text
absolute_effective_ms = C + effective_after_seconds * 1000
scale_ms_per_second = 1000
rounding_direction = none_exact
```

每条候选和胜出规则都保留：

- `source`：`v1` / `v2` / `patch`
- `generation`、`declared_clock_ms`
- `raw_time` 与 `time_conversion`
- `arbitration.reason` 和全部候选裁决坐标

裁决顺序为：逻辑时钟相同则来源格式优先级（v2=20，v1=10）；否则比较规则代数，
代数相同再比较声明逻辑时钟。样本中的 `rule-beta` 专门覆盖同时钟、同代数时的
v2 来源优先；`rule-gamma` 覆盖 v1 代数更高而胜出。

v2 墓碑不会从视图删除，而是保留为 `enabled=false`、`tombstone=true` 和
`tombstone_reason`。若规则自身墓碑或其任一上游失效，`invalid_rule_closure`
会把该规则及传递依赖规则标记为隔离。

## 6. 健康状态机

观测排序键为：

```text
(instance_id, seq, -logical_clock_ms, digest ASC)
```

因此同序号优先采用更大的注入逻辑时钟，时钟相同按摘要字典序稳定选择。

- 状态码属于期望集合：`success`，连续失败清零，状态回到 `healthy`。
- 状态码不符：`status_mismatch`，真实失败计数加一。
- 序号空洞：生成 `missing` 补洞决策；它不是真实观测失败，不增加真实连续失败计数。
- 租约代数落后或规则/上游失效：结论为 `quarantined`，不改变健康状态和失败计数。
- 失败数达到 `suspect_threshold` 进入 `suspect`，达到 `lost_threshold` 进入 `lost`。

## 7. 分片屏障

每个分片清单记录 `content_digest`、`record_count` 和每实例
`start_seq/end_seq`。未加载分片的最小 `start_seq` 形成提交屏障：
引擎只能提交 `start_seq - 1` 之前的结论，后续分片记录进入
`pending_buffer_digest` 缓冲区。中间分片到齐后，所有分片按相同排序规则重放，
得到与一次性 JSONL 完全相同的规范账本。

`artifacts/acceptance-report.json` 记录全部 6 种三分片排列，所有哈希均等于
基线哈希。

## 8. SQLite、崩溃恢复与并发

SQLite 保存：

- 注册实例和租约/状态世代；
- 观测候选及 `UNIQUE(instance_id, seq, digest)`；
- 分片清单与加载状态；
- `pending_batches` / `pending_batch_items` 半提交缓冲区；
- 规则视图、补丁和不可变 `rule_revisions(rule_key, generation)`；
- `ledger_versions`、版本化 `ledger_entries`、`ledger_decisions`。

提交账本时先写 `temp.ledger_stage_entries` 和
`temp.ledger_stage_decisions`，取得新版本号后复制到版本表，最后在同一事务中
更新 `meta.current_ledger_version` 指针。读取方只读取已提交指针指向的
`ledger_versions.canonical_json`。

`workbench.storage.Storage` 使用 `fcntl.flock(db.lock)` 串行化多进程写事务，
SQLite 同时启用 `busy_timeout`。相同观测摘要靠唯一键幂等；相同 `patch_id`
直接返回首次结果；相同 `(rule_key, generation)` 若内容哈希不同则返回
`E_RULE_GENERATION_CONFLICT`。

崩溃注入点：

- `WB_CRASH_POINT=after_pending_commit`：观测缓冲区已事务提交、CLI 尚未返回时 `_exit(137)`。
- `WB_CRASH_POINT=after_stage_before_pointer`：stage 和新版本已写入但事务尚未 COMMIT 时 `_exit(137)`；SQLite 回滚该半事务。

恢复命令 `python -m workbench resume` 识别 `status=buffered` 的批次，从已提交
`committed_through_seq` 之后重放，因此不会重复递增失败计数。

## 9. 实测结果

最终一次完整运行：

```text
python3 -m unittest tests.test_workbench
Ran 11 tests ... OK

bash scripts/acceptance.sh
ACCEPTANCE_OK
```

关键结果（详见 `artifacts/acceptance-report.json`）：

- 基线规范账本哈希：`2b93d5e6d8eb3b5880482cdb1a22da2f2dc2ea4cefd461415fc7c15fde3f6ec8`
- 6 种分片排列：全部 `matches_baseline=true`
- 中间分片缺失：`inst-alpha` 只提交到 seq=2，seq>2 的决策数为 0
- 缓冲区崩溃：进程退出码 137，恢复前识别到 1 个 buffered batch，恢复后哈希一致
- 指针提交前崩溃：进程退出码 137，未提交事务回滚，基线哈希不变
- 4 个分片加载进程（含重复 shard-001）：退出码均为 0，最终哈希一致
- 额外 8 进程并发同一补丁：1 个实际应用、7 个幂等返回，`patches` 表仅 1 行
- Flask `GET /api/status` 与 `/`：均返回 HTTP 200

## 10. 验证失败输出

当两份账本不一致时：

```bash
python -m workbench verify --expected a.json --actual b.json
```

退出码为 `20`，错误码为 `E_RESULT_MISMATCH`，`details` 中给出
`first_instance_id` 与 `first_observation_seq`，并附带最多 20 条字段级差异。
