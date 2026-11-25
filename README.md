# 目标检测候选匹配验证组件

确定性、可集成的候选框-真值框匹配验证组件。链路：**样本摄入与校验 → 按类别分组贪心指派 → 双版本匹配账本写入与快照 → 账本重放 → 结果验证**。仅依赖 `numpy` 与 Python 标准库（`sqlite3`、`json`、`hashlib`），不需要真实图像、在线标注或 GPU。

## 目录结构

```
generate_samples.py     固定种子样本生成器（numpy RandomState，默认 seed=20261126）
example.py              端到端完整示例（覆盖验收全项）
matcher/                可集成组件包
  config.py             Config：阈值、受控标签集、资源上限、逻辑时钟；越界校验
  errors.py             BusinessError + 全部结构化业务码
  ingest.py             JSONL 解析、E_DUP_IMAGE/E_BAD_BOX 校验、稳定排序、字节级一致 IR
  match.py              IoU 明细、按类别分组贪心指派、逐项比较审计日志、类别聚合
  ledger.py             v1 单文件 JSON / v2 分片 JSON+SQLite 读写与版本头分派
  migrate.py            v1↔v2 双向迁移、补全来源记录、降级记录、E_MIGRATION_MISMATCH
  cache.py              内容寻址缓存键、受影响图像计算、修订号回退检查
  snapshot.py           版本化快照（单调版本+校验和）、批次原子提交、单提交者锁
  replay.py             行级/图像级/跨版本重放，E_REPLAY_MISMATCH
  selfcheck.py          跨模块一致性自检，E_INCONSISTENT
  component.py          Pipeline 门面：run / apply_update / verify_snapshots / read_back
tests/test_component.py 24 个单元与集成测试
samples/ ledgers/ checkpoints/   运行期产物目录
```

## 快速开始

```bash
python3 generate_samples.py            # 生成确定性样本到 samples/
python3 example.py                     # 端到端演示（含错误码注入）
python3 -m unittest discover -s tests  # 24 项测试
```

集成方式：

```python
from matcher import Pipeline, Config
p = Pipeline(".", Config(iou_threshold=0.5, batch_size=4))
report = p.run("samples/candidates.jsonl", "samples/ground_truth.jsonl")
p.apply_update(gt_path="samples/ground_truth_v2.jsonl")   # 真值修订 → 定向失效
p.apply_update(iou_threshold=0.6)                          # 阈值调整 → 全量失效
p.apply_update(migrate_to="v1")                            # 整体降级后再增量
```

## 关键设计

- **确定性**：所有浮点 `round(6)`、JSON `sort_keys`+紧凑分隔符、排序键 `(image_id, box_idx)`；同输入同配置下 IR 与 v1 账本逐字节一致。
- **贪心指派**：图像内按 `class_id` 分组，候选按 `(-score, x1, y1, box_idx)` 排序，依 `box_idx` 序尝试未占用真值，首个 IoU≥阈值即 TP；每次比较记录 `inter/union/iou/result` 于 `checkpoints/matches.json`，任意指派可逐项复算。
- **账本双格式**：v1 单文件 JSON（`t_rel_s` 相对提交秒、扁平行、聚合分数+行校验和）；v2 `ledgers/v2/shards/shard_XX.json` + `meta.sqlite3`（`t_abs_ms` 绝对逻辑毫秒、`generation` 匹配代数、`cand_hash`、`gt_revision`、`row_hash`、分片校验和）。读取器按版本头分派。
- **迁移**：升级补全 `generation=0 / rev0 / 候选哈希 / 绝对时钟` 并逐项写入 `migration_log`（来源：`computed:candidate-content`、`default:gen0`、`default:rev0`、`derived:relative-clock`）；降级按 `drop-v2-only-fields` 规则显式丢弃并写 `ledgers/downgrade_log.json`；双向迁移后 TP/FP/FN 多重集合与聚合分数必须一致。
- **定向失效**：缓存键 = `sha256(图像标识+候选内容+真值内容(含 revision)+阈值+标签集)`；真值修订或阈值变化仅使内容变化的图像失效，未受影响图像复用已提交结果，受影响图像 `generation+1` 重写账本行；格式演进（v1↔v2）强制先整体迁移再增量。
- **快照与恢复**：v1/v2 账本、真值清单、配置各持单调版本号+SHA-256 校验和，变化即整体替换（临时文件+rename）；批次边界原子提交（`checkpoints/batches/`+`HEAD`+输入指纹 `batch_key`），重启从 `HEAD+1` 继续且已提交图像不重算；`commit.lock`（O_EXCL）保证单提交者。
- **资源上限**：`max_resident_candidates` / `max_open_shards` / `max_recompute_images` 可配；超限返回 `E_RESOURCE_LIMIT` 或（`allow_degrade=True` 时）降级并在报告 `degraded` 中标记。

## 业务码

| 代码 | 触发 | 关键 field |
|---|---|---|
| `E_DUP_IMAGE` | 候选/真值 (image_id, box_idx) 重复 | `image_id` |
| `E_BAD_BOX` | 负坐标、退化框、标签越集、缺字段 | 具体字段 |
| `E_VERSION_UNSUPPORTED` | 未知账本版本头 | `version` |
| `E_FORMAT_CORRUPT` | 字段缺失/数值不可解析/分片校验和错 | 具体字段 |
| `E_MIGRATION_MISMATCH` | 迁移后两代 TP/FP/FN 或聚合不一致 | `rows`/`aggregate` |
| `E_REPLAY_MISMATCH` | 行/图像/跨版本重放不符 | `iou`/`rows`/`cand_hash` |
| `E_SNAPSHOT_ROLLBACK` | 快照/批次版本回退 | `version`/`batch_id` |
| `E_SNAPSHOT_CORRUPT` | 快照校验和不符 | `checksum` |
| `E_STAGE_BUSY` | 并发提交冲突 | `lock` |
| `E_RESOURCE_LIMIT` | 驻留候选/分片/重算超限 | 对应上限字段 |
| `E_INCONSISTENT` | 自检：行数/集合/重放互推失败 | `consistency` |
| `E_THRESHOLD_RANGE` | 阈值越界 [0,1] | `iou_threshold` |
| `E_REVISION_REGRESS` | 真值修订号回退 | `revision` |

## 实际验证结果（seed=20261126，12 图 / 54 候选 / 30 真值）

- 全链路：v1/v2 各 **59 行**（TP=25, FP=29, FN=5），类别聚合一致；重放 12 图 59 行全通过，自检 `consistent`。
- 确定性：两次独立运行 v1 账本 **9177 字节逐字节一致**。
- 中断恢复：batch_size=4 时在 batch 1 后注入崩溃，重启 `resumed_from_batch=1`，仅重算 4 图，已提交 8 图零重算。
- 并发：持锁后第二个提交立即 `E_STAGE_BUSY`。
- 定向失效：`ground_truth_v2.jsonl`（img_0002/img_0007 修订号 1→2）仅使该 2 图失效、复用 10 图；阈值 0.5→0.6 使全部 12 图失效。
- 迁移：v2→v1 降级 62 行并写降级记录；v1→v2 升级记录 208 条补全来源；迁移后两代 TP/FP/FN 多重集合与聚合分数一致。
- 错误注入：`E_DUP_IMAGE / E_BAD_BOX / E_VERSION_UNSUPPORTED / E_FORMAT_CORRUPT / E_THRESHOLD_RANGE / E_SNAPSHOT_ROLLBACK / E_SNAPSHOT_CORRUPT / E_RESOURCE_LIMIT(×2) / E_REVISION_REGRESS` 均按预期返回并指明字段。
- `python3 -m unittest discover -s tests`：**24 项测试全部通过**。
