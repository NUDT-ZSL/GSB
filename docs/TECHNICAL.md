# 韵律边界标注诊断应用技术说明

## 1. 运行入口

环境：Python 3.10+、NumPy、内置 `sqlite3`。无需 Docker、GPU、网络或外部音频服务。

```bash
pip install -r requirements.txt
python run.py --mode generate     # 可选；full 发现缺少样本时也会自动生成
python run.py --mode full
bash scripts/verify_acceptance.sh  # 完整本地验收
python -m unittest tests.test_diagnostics
```

常用注入：

```bash
python run.py --mode full --precision fp32 --tree-version both
python run.py --mode full --precision fixed:scale=4096,rounding=nearest_away
PROSODY_PRECISION=float32 PROSODY_TREE_VERSION=v2 \
  PROSODY_INTERRUPT_AT=adjudication:2 python run.py --mode full
python run.py --mode full --interrupt-at features:3
python run.py --mode trace --utterance U001
python run.py --mode interactive
```

支持的环境变量：`PROSODY_PRECISION`、`PROSODY_TREE_VERSION`、`PROSODY_INTERRUPT_AT`、
`PROSODY_SAMPLE_DIR`、`PROSODY_OUTPUT_DIR`。

## 2. 确定性样本

固定种子 `20261127`，生成：

- `samples/audio/SPK01.pcm` 等：16 kHz、单声道、signed little-endian int16。
- `samples/tasks.jsonl`：六个 utterance、三个说话人，含题目要求字段及内部 `revision`。
- `samples/frame_truth.jsonl`：1434 帧真值，字段含 `frame_index/speaker_id/f0_true/energy_true/pause_label`。
- `samples/boundary_truth.jsonl`：停顿边界参考标注。
- `samples/trees/v1/prosody_tree_v1.json`：v1 单文件、相对秒、七段竖线扁平规则。
- `samples/trees/v2/`：三个规则分片、`manifest.json` 和 SQLite `meta/prosody.db`。

默认 utterance 时长 2400 ms，对应 38400 个样本；帧长 320 samples（20 ms），帧移 160
samples（10 ms），无填充帧数严格为 `1 + (38400 - 320) / 160 = 239`。

## 3. 处理阶段

1. `prosody_diag/audio.py`：按样本索引切帧，计算 RMS 能量、自相关 F0 候选、过零率，
   合并停顿时长并生成 `pause_onset/pause_offset/f0_reset/energy_reset/zcr_peak` 候选。
2. `prosody_diag/trees.py`：版本头/字段集分派解析 v1/v2，规范化为同一内部规则结构，
   执行迁移和精度上下文推理。
3. `prosody_diag/adjudicate.py`：输出逐候选 v1/v2 类别、置信度、决策路径、规则版本和冲突，
   按 `truth > v2 > v1`、同源规则号升序合并账本。
4. `prosody_diag/errorspec.py`：计算特征规范化、节点 margin、置信度累加、分类 margin
   四级绝对/相对偏差、误差区间、放大比、主来源和门限稳定性。
5. `prosody_diag/cache.py` 与 `prosody_diag/pipeline.py`：内容寻址缓存、定向失效和四阶段提交。
6. `prosody_diag/state.py`：阶段快照、单调版本、内容校验和、`flock(LOCK_EX|LOCK_NB)` 提交锁。

## 4. 精度语义

`PrecisionContext` 对三档暴露同一组 `put/add/sub/mul/div/sqrt/ge/...` 算子和相同运算顺序：

- `fp64` / `float64`：`numpy.float64` 参考路径。
- `fp32` / `float32`：每个声明中间结果显式转回 `numpy.float32`。
- `q15` / `fixed`：默认 `scale=32768`、`nearest_even`；也支持
  `fixed:scale=<int>,rounding=nearest_even|nearest_away|truncate`。

候选集合只由确定性原始声学结构生成，不随精度档变化。测试断言全部 54 个候选的三档 ID、
遍历条件结果、选中规则和标签一致；39 个近门限候选在三档重复运行下均无舍入翻转。

## 5. 双版本树与迁移

v1 规则行固定七段：

```text
R001|0.0|pause_norm|>=|0.62|B3|0.95
```

v2 使用分片 JSON 保存绝对毫秒和条件树，SQLite `rule_meta` 保存 `generation/active/tombstone/
first_seen_ms/updated_ms`。升级 v1→v2 时补全：

- `generation=1`，来源 `default:generation=1`；
- `tombstone=null`，来源 `default:null`；
- `time_ms=round(time_sec * 1000)`，来源 `conversion:relative_seconds*1000`。

降级时，AND/OR 嵌套按 root-to-leaf 路径展开为 DNF 扁平规则；墓碑按显式
`tombstone_policy="discard"` 丢弃。严格全保真模式遇到墓碑或不可表达结构返回
`E_SEMANTIC_LOSS`。实测 v1→v2、v2(迁移件)→v1 的内部规则集哈希和边界序列哈希完全相同。

## 6. 缓存与定向失效

决策缓存键包含：utterance `revision`、PCM 片段 SHA-256、声学上游键、精度 profile 规范、
树格式版本、`tree_hash`、`decision_tree_rev`、`rule_generation` 和规则/裁决器版本。

`state/key_registry.json` 保存逻辑产物到内容键的映射，`cache/cache_index.json` 保存可追溯
元数据（精度档、树版本、使用/命中规则等）。修订号提升时，只有该 utterance 的特征、裁决和误差
键变化；规则置信度修订通过依赖图只使 `matched_rule_ids` 含该规则的 utterance 失效，阈值修订
则使用 `evaluated_rule_ids` 传播。

示例：

```bash
python run.py --mode full --output-dir /tmp/inv
python run.py --mode full --output-dir /tmp/inv --bump-revision U002
python run.py --mode full --output-dir /tmp/rule --change-rule R004.confidence=0.91
python run.py --mode full --output-dir /tmp/prof --switch-profile U002=q15
```

实测修订、声明精度档切换均只重算 `U002`，复用
`U001,U003,U004,U005,U006`；规则置信度修订按实际依赖规则得到更大的定向集合。

## 7. 状态机与恢复

阶段顺序固定为 `features -> adjudication -> error_spectrum -> report`。每个阶段完成全部
utterance 后才写版本化快照和 `*.latest.json`；JSON/JSONL 均先写同目录临时文件、fsync，再
`os.replace` 原子替换。快照含单调 `version`、`completed_utterance_ids`、
`next_utterance_id` 和 `content_sha256`。

`--interrupt-at <stage>:<N>` 在处理 N 个 utterance 后调用 `os._exit(37)`，模拟强杀。重启后：

- 已提交阶段快照直接跳过；
- 已原子写入的内容缓存按哈希复用；
- 未提交的 `*.tmp.*.partial` 被清理；
- 最终账本/报告由全部缓存明细重新汇总并原子替换，不会追加重复账本条目。

自动化测试将无中断目录与 `adjudication:2` 强杀/恢复目录递归比较；除运行遥测
`last_run_plan.json` 外，所有状态、缓存、快照和报告字节一致。

## 8. 错误码

| 业务码 | 触发点 |
| --- | --- |
| `E_FRAME_COUNT_MISMATCH` | PCM 字节跨度、样本数、真值帧数或精确切帧公式不一致 |
| `E_VERSION_UNSUPPORTED` | 未知树版本头/不可识别字段集 |
| `E_FORMAT_CORRUPT` | 缺字段、竖线字段数错、SQLite/JSON 元数据时间戳倒退 |
| `E_SEMANTIC_LOSS` | 严格迁移时遇到墓碑、否定嵌套等 v1 无法全保真表达的结构 |
| `E_CONFLICT_OVERFLOW` | 跨版本冲突候选数超过 `conflict_threshold` |
| `E_TOLERANCE_EXCEEDED` | 任一级误差超容差，详情给 `first_position/level` |
| `E_STAGE_BUSY` | 非阻塞提交锁失败，后到调用立即返回 |
| `E_DUPLICATE_UTTERANCE_ID` | 任务清单 utterance ID 重复 |
| `E_INVALID_FRAME_CONFIG` | 帧长/帧移/采样率或树参数非法 |
| `E_PRECISION_PROFILE_UNKNOWN` | 精度档名称或定点 scale/rounding 非法 |
| `E_REFERENCE_MISMATCH` | 参考边界找不到候选或标签不受支持 |
| `E_TREE_VERSION_ROLLBACK` | `decision_tree_rev`/`logical_clock_ms` 低于已提交高水位 |

## 9. 资源上限与降级

`Config` 提供 `max_utterances_per_run=12`、`max_resident_frames=4096`、
`max_cache_entries=512`。超限时按顺序记录：

- `batch_streaming`：缩小单批 utterance 数并分批推进；
- `feature_streaming`：特征写入内容缓存后释放帧数组；
- `cache_prune_threshold`：提高候选 score 剪枝阈值以减少驻留/缓存条目。

测试用极小上限断言三类降级均会落盘到报告的 `resource_degradations`。

## 10. 主要产物

默认输出目录为 `artifacts/`：

- `frame_features.jsonl`：逐帧声学特征。
- `boundary_candidates.jsonl`：候选边界及原始归一化量。
- `candidate_judgments.jsonl`：逐候选完整判定明细和决策路径。
- `annotation_ledger.jsonl`：可由判定明细重算的统一账本。
- `error_spectrum.json`：逐帧/逐候选误差明细、统计和稳定性记录。
- `diagnostic_report.json` / `diagnostic_report.md`：诊断报告。
- `state/*.latest.json`、`state/snapshots/`：阶段快照和高水位。
- `cache/objects/`、`cache/cache_index.json`：内容寻址缓存。
- `migration/`：迁移件、补全来源和降级报告。
