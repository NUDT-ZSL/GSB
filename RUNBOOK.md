# 运行说明与验收命令

以下命令均从仓库根目录执行。退出码约定：

- `0`：成功，或演示场景得到预期结果。
- `2`：已归因业务错误，例如格式、快照、资源或语义损失。
- `1`：未归因异常。

## 1. 环境

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install -e .

python --version
pip --version
python -c "import numpy; print(numpy.__version__)"
```

## 2. 生成固定数据

```bash
python -m intent_interop generate --out-dir data
```

生成 25 条 `queries.jsonl`，字段严格为 `query_id`、`query_text`、`locale`、`label_raw`。同时生成：

| 文件 | 用途 |
| --- | --- |
| `data/config_v1.txt` | v1 竖线配置 |
| `data/config_v2.txt` | v2 键值行式 |
| `data/config_v3.json` | v3 结构化配置 |
| `data/config_v3_rich.json` | 含层级、槽位、非均匀权重的富语义配置 |
| `data/config_v2_snapshot2_retired.txt` | 版本 2，且 `refund_request` 退休 |
| `data/config_v2_same_version_replaced.txt` | 同版本、不同校验和 |
| `data/config_v2_corrupt.txt` | 校验和损坏 |
| `data/config_v1_bad_fields.txt` | v1 行字段数错误 |
| `data/config_v2_missing_key.txt` | v2 缺键 |
| `data/config_v1_keyword_overflow.txt` | 关键词总数超限 |
| `data/queries_dirty.jsonl` | 缺 `locale` 的脏样本 |
| `data/queries_duplicate.jsonl` | 重复 `query_id` |
| `data/queries_overlong.jsonl` | 超过查询码点上限 |

## 3. 基线：三格式一致性裁决

```bash
python -m intent_interop baseline \
  --samples data/queries.jsonl \
  --v1 data/config_v1.txt \
  --v2 data/config_v2.txt \
  --v3 data/config_v3.json \
  --report reports/baseline.json
```

预期：退出码 `0`，报告状态 `OK`，25 条查询全部 `consistent=true`。

报告：`reports/baseline.json`

## 4. 单配置判定

```bash
python -m intent_interop match \
  --samples data/queries.jsonl \
  --config data/config_v1.txt \
  --report reports/match_v1.json

python -m intent_interop match \
  --samples data/queries.jsonl \
  --config data/config_v2.txt \
  --report reports/match_v2.json

python -m intent_interop match \
  --samples data/queries.jsonl \
  --config data/config_v3.json \
  --report reports/match_v3.json
```

预期：三份报告均为 `OK`，`match.self_check.covered_count=25`。

## 5. 双向迁移

```bash
python -m intent_interop migrate \
  --config data/config_v1.txt --from-version v1 --to v2 \
  --out data/migrated_v1_to_v2.txt --samples data/queries.jsonl \
  --report reports/migrate_v1_to_v2.json

python -m intent_interop migrate \
  --config data/config_v2.txt --from-version v2 --to v3 \
  --out data/migrated_v2_to_v3.json --samples data/queries.jsonl \
  --report reports/migrate_v2_to_v3.json

python -m intent_interop migrate \
  --config data/config_v3.json --from-version v3 --to v2 \
  --out data/migrated_v3_to_v2.txt --samples data/queries.jsonl \
  --hierarchy-policy drop --loss-policy allow \
  --report reports/migrate_v3_to_v2.json

python -m intent_interop migrate \
  --config data/config_v2.txt --from-version v2 --to v1 \
  --out data/migrated_v2_to_v1.txt --samples data/queries.jsonl \
  --report reports/migrate_v2_to_v1.json
```

预期：四份报告均为 `OK`，动作记录包含补全或丢弃明细。

富语义降级：

```bash
python -m intent_interop migrate \
  --config data/config_v3_rich.json --from-version v3 --to v2 \
  --out data/migrated_v3_rich_to_v2.txt --samples data/queries.jsonl \
  --hierarchy-policy drop --loss-policy allow \
  --report reports/migrate_v3_rich_to_v2_loss.json

python -m intent_interop migrate \
  --config data/config_v3_rich.json --from-version v3 --to v2 \
  --out data/migrated_v3_rich_rejected.txt \
  --hierarchy-policy drop --loss-policy reject \
  --report reports/migrate_v3_rich_to_v2_reject.json
```

预期：两份均返回 `E_SEMANTIC_LOSS`。`allow` 场景仍写出降级配置并保留损失明细；`reject` 场景不写出目标配置。

## 6. 三阶段异常注入

```bash
python -m intent_interop inject \
  --samples data/queries.jsonl --config data/config_v1.txt \
  --inject read:field_count \
  --report reports/inject_read_field_count.json

python -m intent_interop inject \
  --samples data/queries.jsonl --config data/config_v1.txt \
  --inject migrate:missing_key \
  --report reports/inject_migrate_missing_key.json

python -m intent_interop inject \
  --samples data/queries.jsonl --config data/config_v1.txt \
  --inject match:empty_keyword \
  --report reports/inject_match_empty_keyword.json

python -m intent_interop inject \
  --samples data/queries.jsonl --config data/config_v1.txt \
  --inject match:offset_length \
  --report reports/inject_match_offset_length.json

python -m intent_interop inject \
  --samples data/queries.jsonl --config data/config_v1.txt \
  --inject match:unexpected \
  --report reports/inject_match_unexpected.json
```

预期状态依次为：

- `E_FORMAT_CORRUPT`，带 `line_no=6`
- `E_FORMAT_CORRUPT`，带迁移阶段与 `line_no`
- `E_CONFIG_INVALID`，带 `query_id=q001`
- `E_INCONSISTENT`，类别 `offset_table_length`
- `E_UNATTRIBUTED`，记录只出现在 `unattributed_errors`

## 7. 快照替换

标签收缩回退：

```bash
python -m intent_interop snapshot \
  --samples data/queries.jsonl \
  --initial data/config_v2.txt \
  --replacement data/config_v2_snapshot2_retired.txt \
  --retired-policy fallback \
  --report reports/snapshot_fallback.json
```

预期：`OK`；快照 `snapshot_version=2`、`generation=2`、`replacement_mode=whole_snapshot`；6 条受影响查询回退；未受影响查询无变化；旧快照回写触发 `E_SNAPSHOT_ROLLBACK`。

显式拒绝：

```bash
python -m intent_interop snapshot \
  --samples data/queries.jsonl \
  --initial data/config_v2.txt \
  --replacement data/config_v2_snapshot2_retired.txt \
  --retired-policy reject \
  --report reports/snapshot_reject.json
```

预期：`E_LABEL_RETIRED`，带首个受影响 `query_id`。

同版本换校验和：

```bash
python -m intent_interop snapshot \
  --samples data/queries.jsonl \
  --initial data/config_v2.txt \
  --replacement data/config_v2_same_version_replaced.txt \
  --retired-policy fallback \
  --report reports/snapshot_same_version_replacement.json
```

预期：`OK`，原因 `checksum_changed_same_version`，仍为整体替换。

损坏快照：

```bash
python -m intent_interop snapshot \
  --samples data/queries.jsonl \
  --initial data/config_v2.txt \
  --replacement data/config_v2_corrupt.txt \
  --report reports/snapshot_corrupt.json
```

预期：`E_SNAPSHOT_CORRUPT`，带版本 `2` 与 `field_offset`。

## 8. 富语义跨版本损失

```bash
python -m intent_interop baseline \
  --samples data/queries.jsonl \
  --v1 data/config_v1.txt \
  --v2 data/config_v2.txt \
  --v3 data/config_v3_rich.json \
  --report reports/semantic_loss.json
```

预期：`E_SEMANTIC_LOSS`；`semantic_loss.unrestorable_fields` 包含 `label_path`、`slots`、`weight_vector`；`semantic_loss.query_ids` 列出命中受影响意图的查询；`conclusion=null`。

## 9. 资源、输入与格式异常

```bash
python -m intent_interop match \
  --samples data/queries.jsonl \
  --config data/config_v1_keyword_overflow.txt \
  --max-keywords 64 \
  --report reports/resource_keyword_limit.json

python -m intent_interop match \
  --samples data/queries.jsonl \
  --config data/config_v1_keyword_overflow.txt \
  --max-keywords 64 --resource-policy prefix \
  --report reports/resource_prefix_degrade.json

python -m intent_interop match \
  --samples data/queries_overlong.jsonl \
  --config data/config_v1.txt \
  --max-query-codepoints 256 \
  --report reports/resource_query_length.json

python -m intent_interop match \
  --samples data/queries_duplicate.jsonl \
  --config data/config_v1.txt \
  --report reports/input_duplicate_query.json

python -m intent_interop match \
  --samples data/queries_dirty.jsonl \
  --config data/config_v1.txt \
  --report reports/input_dirty_sample.json

python -m intent_interop match \
  --samples data/queries.jsonl \
  --config data/config_v1_bad_fields.txt \
  --report reports/config_bad_field_count.json

python -m intent_interop match \
  --samples data/queries.jsonl \
  --config data/config_v2_missing_key.txt \
  --report reports/config_missing_key.json
```

预期状态依次为：

- `E_RESOURCE_LIMIT`（关键词总数）
- `OK` 且含 `keyword_budget_prefix_only` 降级类别
- `E_RESOURCE_LIMIT`（查询码点）
- `E_DUP_QUERY`，带重复 `query_id` 与行号
- `E_CONFIG_INVALID`，字段 `locale`
- `E_FORMAT_CORRUPT`，行号 6
- `E_FORMAT_CORRUPT`，缺失键 `fallback_intent`

## 10. 一键验收

```bash
python -m intent_interop demo \
  --data-dir data \
  --report-dir reports
```

预期输出类似：

```json
{"failed": 0, "passed": 27, "report": "reports/demo_summary.json", "status": "OK"}
```

## 11. 自动测试

```bash
python -m unittest discover -s tests -v
```

预期：29 项测试全部通过。

## 12. 主要报告文件

| 报告 | 预期状态 |
| --- | --- |
| `reports/baseline.json` | `OK` |
| `reports/match_v1.json` / `match_v2.json` / `match_v3.json` | `OK` |
| `reports/migrate_v1_to_v2.json` 等四向迁移 | `OK` |
| `reports/migrate_v3_rich_to_v2_loss.json` | `E_SEMANTIC_LOSS` |
| `reports/inject_read_field_count.json` | `E_FORMAT_CORRUPT` |
| `reports/inject_migrate_missing_key.json` | `E_FORMAT_CORRUPT` |
| `reports/inject_match_empty_keyword.json` | `E_CONFIG_INVALID` |
| `reports/inject_match_offset_length.json` | `E_INCONSISTENT` |
| `reports/inject_match_unexpected.json` | `E_UNATTRIBUTED` |
| `reports/snapshot_fallback.json` | `OK` |
| `reports/snapshot_reject.json` | `E_LABEL_RETIRED` |
| `reports/snapshot_same_version_replacement.json` | `OK` |
| `reports/snapshot_corrupt.json` | `E_SNAPSHOT_CORRUPT` |
| `reports/semantic_loss.json` | `E_SEMANTIC_LOSS` |
| `reports/resource_keyword_limit.json` | `E_RESOURCE_LIMIT` |
| `reports/resource_prefix_degrade.json` | `OK` |
| `reports/resource_query_length.json` | `E_RESOURCE_LIMIT` |
| `reports/input_duplicate_query.json` | `E_DUP_QUERY` |
| `reports/input_dirty_sample.json` | `E_CONFIG_INVALID` |
| `reports/config_bad_field_count.json` | `E_FORMAT_CORRUPT` |
| `reports/config_missing_key.json` | `E_FORMAT_CORRUPT` |
| `reports/demo_summary.json` | `OK` |
