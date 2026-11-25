# 本地风险治理诊断管线设计

## 1. 交付结构

- `generate_sample.py`：固定种子入口，生成样本、基线配置、生效配置和变更事件。
- `risk_governance/generator.py`：确定性样本与配置生成实现，默认种子 `416`。
- `risk_governance/stats.py`：分组、值域校验、数值统计、类别占比、参考窗口与全量窗口。
- `risk_governance/calibration.py`：分段分位锚点、单调性检查、线性插值和错误码。
- `risk_governance/boundary.py`：最近边界/异质样本距离、跨周期翻转、迟滞带修复。
- `risk_governance/cache.py`：SHA-256 内容缓存、缓存键和哈希引用链。
- `risk_governance/events.py`：变更维度解析、传递闭包、重算/复用覆盖检查。
- `risk_governance/recovery.py`：阶段索引、批次提交、临时产物和重启清理。
- `risk_governance/snapshots.py`：v1 单文件快照、v2 分片 + SQLite 快照、原子切换和回退拒绝。
- `risk_governance/pipeline.py`：端到端编排、报告和信号中断处理。
- `risk_governance/cli.py`：`run`、`verify`、`replay`、`snapshot-rollback` 命令。
- `tests/test_pipeline.py`：9 个 `unittest` 验收与故障注入用例。
- `scripts/verify_acceptance.sh`：一键验收脚本。

## 2. 快速运行

```bash
python3 -m pip install -r requirements.txt
python3 generate_sample.py --out-dir data/generated
bash scripts/verify_acceptance.sh
```

常用分步命令：

```bash
python3 -m risk_governance.cli run \
  --config data/generated/calibration_config.baseline.json \
  --events '' --workdir out/demo-baseline \
  --now-ms 1764000000000 --seed 416

python3 -m risk_governance.cli run \
  --config data/generated/calibration_config.json \
  --events data/generated/change_events.jsonl \
  --workdir out/demo-baseline \
  --now-ms 1764000000000 --seed 416

python3 -m risk_governance.cli verify --workdir out/demo-baseline
python3 -m risk_governance.cli replay \
  --report out/demo-baseline/reports/execution_report.json
```

重复执行时固定 `--now-ms` 可使报告和快照时间戳完全可复现；省略该参数则使用绝对墙钟毫秒，并把实际值写入 `replay_parameters.now_ms`。

## 3. 输入模型

样本 JSONL 每行包含：

```json
{
  "batch_id": "B000",
  "cycle_id": "C00",
  "entity_id": "G00-E0000",
  "features": {"age": 41, "amount": 1234.56, "region": "east"},
  "score": 0.63,
  "label": 1
}
```

读取后按 `(batch_id, cycle_id)` 的自然序排序和分组。兼容别名字段 `decision_score`、`true_label`、`feature_columns`。

事件字段为 `event_seq`、`batch_id`、`change_dimension`、`old_config_summary`、`new_config_summary`，也兼容 `dimension`、`seq`、`old_summary`、`new_summary`。

## 4. 统计与校验

每个批次维护：

- 记录数、样本数、样本内容哈希和批次指纹。
- 数值特征的计数、缺失/非法/越域计数、均值、总体方差、标准差、最小值、最大值和分位数。
- 类别特征的计数、唯一值、类别计数和占比。
- `score` 的独立数值统计与 `label` 分布。

校验异常包括样本数不满足最小值/期望值、缺失特征、非法数值、数值越界、类别不在允许域、分数越界和标签非法。异常计数和证据写入批次账本与报告。

输出显式区分：

- `window_statistics.reference_window`：由 `reference_cycle_ids` 选择，默认生成数据为 `C00`。
- `window_statistics.full_window`：全部周期和批次。

批次摘要是统计结果的规范化 SHA-256；冷启动独立工作区中的 `ledger/batch_statistics_ledger.json` 已验证逐字节一致。

## 5. 分段分位校准

每个校准项由 `anchor_probabilities`、`target_anchor_values` 和若干 `segments` 组成。对每个分段：

1. 从批次分位数中取左右源锚点。
2. 锚点缺失时记录 `E_QUANTILE_ORDER` 和 `reason=missing_quantile`，不会把缺失值当零。
3. 左右源值或目标值倒挂时记录 `E_QUANTILE_ORDER`，包含 `batch_id`、`cycle_id`、`item`、`segment_index`。
4. 源区间分母为零时记录 `E_ZERO_DENOM`，包含批次和分段索引。
5. 校验通过后计算 `slope`、`intercept`，并支持把样本分数映射为校准分。

## 6. 内容缓存与引用链

缓存键由以下规范化 JSON 的 SHA-256 得到：

- `upstream_batch_sample_content_hash` / `sample_content_hash`
- `algorithm_version`
- `segment_parameter_version`
- `algorithm_and_segment_parameter_version`
- `calibration_event_seq` / `event_seq`
- `subordinate_snapshot_version` / `dependent_snapshot_version`
- 额外的 `batch_id`、`cycle_id`

对象存为 `cache/objects/<key前两位>/<key>.json`。键相同直接读取缓存产物；任一致赖变化导致键不同则计算新对象，不覆盖旧对象。

`cache/reference_chain.jsonl` 是哈希链，每次批引用键变化追加：

- 新旧键、事件序号、原因、快照版本。
- `prev_hash` 和本条 `entry_hash`。

`verify` 会重算整条链。

## 7. 定向重算闭包

事件闭包从以下种子计算：

- 事件自身的 `batch_id`。
- `affected_batch_ids` 显式清单。
- `scope=cycle` 时的同周期批次。
- `scope=all`、`batch_id=*` 或全局维度时的全量批次。
- 特征维度在无显式范围时按 `config.feature_batches` 展开；未提供索引才保守覆盖所有含该特征的批次。
- `config.lineage.downstream_by_batch` 或 `batch_dependencies` 中的传递下游。

计划输出：

- `event_plan.events[].closure_batch_ids`
- `planned_recompute_set`
- `reused_set`
- `coverage_check`：并集、缺失、多余、交集、是否完全覆盖。
- `event_closure_check`：事件闭包与实际重算集合的双向差异。

生成数据从基线配置演进到生效配置时，只重算 `B002`、`B003`、`B004`、`B006`，复用其余 5 批。

## 8. 类别边界稳定性

每个 `boundary_targets` 输出：

- 距配置阈值最近的样本点及距离。
- 阈值下方/上方最近点。
- 排序后相邻异标签样本的最近边界对距离和回放引用。
- 每个点到异标签样本的最近邻距离均值/最大值。
- 同一 `entity_id` 跨 `(cycle_id, batch_id)` 的决策翻转次数和完整证据。

翻转次数超过 `max_flips` 返回 `E_BOUNDARY_FLIP`，错误中的 `replay_evidence` 含实体、前后批次/周期、分数和决策。没有可用分数或必需的异标签距离统计时返回 `E_BOUNDARY_STATS`。

修复策略为确定性迟滞带 `hysteresis_band`：阈值上下 `margin` 内保持上一类别。生成样本的两个目标在修复后残余翻转分别降到 1 和 0。

## 9. 中断恢复

阶段索引：

1. `sample_index`
2. `batch_processing`
3. `window_statistics_and_boundary`
4. `snapshots`
5. `execution_report`
6. `complete`

每批处理前写 `state/inflight/<batch>.json` 和 `cache/pending/<batch>.json`，并在 `state/staging/<batch>/` 写中间结果。批次账本、缓存对象和引用落盘后，才把批次加入 `state/recovery.json` 的 `committed_batches`。

SIGINT/SIGTERM/SIGHUP 会转换成 `PipelineInterrupted`，退出前把当前阶段索引、已提交批次和信号写入恢复状态。重启同一计划指纹时：

- 从最近提交点继续。
- 已提交批次从缓存/账本加载，不重新计算、不替换批次账本 inode。
- 未完成批次的 inflight、staging 和 pending 指针被清除，并在报告 `recovery.cleaned` 中列出。

`--interrupt-after-batch N` 提供确定性中断注入；测试也包含真实子进程 SIGTERM。

## 10. 版本化快照

- v1：`snapshots/v1/snapshot.json` 单文件，`os.replace` 原子替换；历史归档在 `versions/v<n>.json`。
- v2：`snapshots/v2/releases/v<n>/` 完整发布目录，包含配置、统计、批次、校准、边界、trace 分片和 `index.sqlite`；SQLite 表包括 `batches`、`calibration`、`errors`、`cache_refs`、`traces`。
- v2 通过临时目录 + 目录原子改名 + 符号链接 `current` 的原子替换发布。读者先 `resolve()` 到不可变版本目录，因此不会混合两个版本的分片。

每个清单包含单调递增 `version`、`created_at_ms` 和 `content_checksum`。内容校验和不变时保留当前版本；变化时版本加一并整体发布完整快照，不做增量补丁。旧版本作为回退目标时，`attempt_rollback` 和 `snapshot-rollback --to-version <old>` 返回 `E_SNAPSHOT_ROLLBACK`，CLI 退出码为 3。

## 11. 报告与重放

`reports/execution_report.json` 包含：

- 输入校验和、种子、`now_ms` 和重放参数。
- 批次摘要、参考/全量统计、跨批检查。
- 事件闭包、重算/复用清单和覆盖检查。
- 校准变化哈希链。
- 边界摘要、业务错误和错误码。
- 快照版本/校验和与 v2 校验结果。
- 恢复说明。
- 顶层 `checksum`，算法为 SHA-256 over canonical JSON。

`decision_traces.jsonl` 提供每行样本的原始分、校准分、阈值带类别、标签、trace ID 和行哈希。`replay` 命令按报告参数在新目录重跑，并比对批次指纹、错误码、trace 校验和及两代快照内容校验和。
