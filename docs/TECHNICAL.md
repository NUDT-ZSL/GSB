# 多说话人会议换流点审计系统

本交付是一个完全离线、确定性的 Python 3.10+ 应用。它从固定种子生成 16 kHz 单声道
PCM 会议样本，完成精确分帧、能量/过零率检测、异常分类、说话人稳定裁决、v1/v2
规则迁移、双版本账本、内容寻址增量重算、六阶段快照恢复、时序自检和本地 Web 演示。

## 快速开始

```bash
# 题目指定依赖；当前实现也兼容本机 NumPy 2.x
python3 -m pip install -r requirements.txt

# 生成固定种子样本：data/sample/meeting.pcm、meeting.wav、sample_manifest.json
python3 scripts/generate_deterministic_sample.py

# 运行完整管道并写双版本账本、快照、审计/诊断报告
PYTHONPATH=src python -m meeting_audit run --workspace workspace/demo

# 精确查询整数毫秒点或语音段
PYTHONPATH=src python -m meeting_audit query --workspace workspace/demo --at-ms 12120
PYTHONPATH=src python -m meeting_audit query --workspace workspace/demo --segment SEG-0006

# 一键深度验收：分块/双路径、迁移、真值、规则、音频修订、恢复、分段稳定性
PYTHONPATH=src python -m meeting_audit verify --deep --workspace workspace/validation

# 本地可交互应用，默认仅监听 127.0.0.1:8765
PYTHONPATH=src python -m meeting_audit demo --workspace workspace/demo --port 8765

# 自动化测试
PYTHONPATH=src python -m pytest -q
```

执行 `pip install -e .` 后，也可直接使用 `meeting-audit ...` 命令。

## 代码地图

- `src/meeting_audit/sample_generator.py`：固定种子 `20261127`、三说话人谐波样本、
  间歇静音、增益跳变和三个注入异常。
- `src/meeting_audit/audio.py`：精确样本帧边界、任意字节分块帧流、浮点/定点特征路径。
- `src/meeting_audit/detector.py`：基于 RMS/能量、ZCR 离散区间和能量比的有状态换流点检测。
- `src/meeting_audit/rules.py`：v1 单文件扁平行、v2 分片 JSON + SQLite、双向迁移和等价判定。
- `src/meeting_audit/identity.py`：自相关音高/ZCR 帧特征、质心聚类、稳定身份注册表。
- `src/meeting_audit/segmentation.py`：分段合并/拆分和父子血统继承。
- `src/meeting_audit/ledger.py`：`ledger_v1.jsonl`、`ledger_v2.jsonl`、公共账本和 SQLite。
- `src/meeting_audit/query.py`：整数毫秒/段编号实时查询索引与批量一致性检查。
- `src/meeting_audit/incremental.py`：真值修正、规则提升、音频修订的失效传播和逐字节重放。
- `src/meeting_audit/state.py`：内容寻址缓存与单调版本六阶段原子快照。
- `src/meeting_audit/selfcheck.py`：身份漂移、虚假换流、重叠换流和跨模块一致性诊断。
- `src/meeting_audit/webapp.py`：零外网依赖的本地 HTML/JSON API 交互演示。
- `tests/`：14 个聚焦测试，覆盖上述验收路径。

## 确定性分帧与检测

配置为 `sample_rate=16000`、`frame_ms=20`、`hop_ms=10`，因此：

- `frame_samples = 320`
- `hop_samples = 160`
- 第 `i` 帧样本区间为 `[i*160, i*160+320)`
- 帧中心时间为 `i*10+10 ms`

`ChunkedFrameFeatureStream` 缓存跨块的 hop 后缀，所以任意 PCM 字节切分都重建相同全局帧。
`TurnDetectionStream` 显式携带 VAD、ZCR 参考档、候选确认和冷却状态；分块只是多次
`push`，不会在块边界重新初始化判定上下文。

固定路径使用 `int64` 平方和与整数平方根四舍五入得到 `rms_q`，ZCR 使用精确符号比较计数；
浮点路径计算归一化浮点 RMS/ZCR 后收敛到同一组离散规范值。测试断言两条路径的换流点
身份集合完全相同。

固定样本产生 11 个换流点，业务分类为：

| 时间 | 事件 | 类型 | 业务码 |
|---:|---|---|---|
| 12120 ms | T0006 | 过快切换 | `BIZ_TURN_TOO_FAST` |
| 16200 ms | T0008 | 单说话人抢占 | `BIZ_SPEAKER_PREEMPT` |
| 21050 ms | T0009 | 静音拉长 | `BIZ_SILENCE_TOO_LONG` |

其余 8 个事件为 `BIZ_TURN_NORMAL`。

## 双版本规则与迁移

v1 文档位于 `rules/v1/rules.json`，使用 `time_second` 相对秒和扁平阈值字段。
升级到 v2 后：

- `time_second` 精确转换为 `at_ms` 绝对毫秒；
- 缺失的 `anomaly_type` 按指标默认策略补全；
- 缺失或默认的 `threshold_source` 改写为可追溯来源；
- 每条规则的 `lineage.completed_fields` 和迁移报告逐项记录补全字段；
- v2 按异常类型写分片 JSON，并写 `rules_v2.sqlite.rule_meta`，其中包含
  `at_ms`、`anomaly_type`、`generation`、`rule_algebra` 和内容键。

v2 降级到 v1 时，`condition.nested_policy`、`advanced`、`lineage.completion_notes`
等嵌套表达按 `DOWNGRADE_DISCARD_PATHS` 显式丢弃，并在 `downgrade_report` 中逐项列出
规则 ID、字段路径和原因。简单指标、比较符、阈值和时间保持不变，因此两代视图对同一批
换流点输出相同业务码。

## 身份稳定性与分段修正

每个语音段聚合帧级自相关音高、log-pitch、ZCR 中位数和能量。首次出现的声纹质心创建
稳定标签 `SPK-0001`、`SPK-0002`、`SPK-0003`，后续重复段距离为 0。固定样本的标签序列为
A/B/C/A/B/C/A/B/C/B/A。

合并或拆分时，新段获得带父段 ID 的血统标识，例如：

- `SEG-0002__L4800`
- `SEG-0002__R4800`
- `SEG-0002__L4800__M__SEG-0002__R4800`

若父段标签一致，子段以 `stable_inherited` 继承；只有新段进入重算集合，未受影响段的
分配对象和标签保持不变。

## 实时查询一致性

`QueryIndex.at_ms(ms)` 只接受非负整数毫秒或纯数字字符串；非整数浮点数会被拒绝，不做
隐式四舍五入。由于 1 ms 对应 16 个样本，`exact_sample_index = ms * 16` 是精确整数运算。

返回内容包括：

- `latest_turn`：该时间点之前最近的换流点；
- `nearest_turn`：绝对距离最近的换流点及确定性平局规则；
- `active_segment_id` 与 `speaker_id`；
- 当前业务码；
- 对应账本溯源引用。

`check_query_batch_consistency` 逐个换流点和语音段比对查询响应与批量结果；重复查询
返回完全相同的字典。

## 增量更新与失效传播

账本行不嵌入易变的全局修订号或全局 PCM 键，而嵌入帧内容键、特征键、段身份分配键和
规则内容键。因此未受影响来源不变时，账本行可以逐字节复用。

典型真值修正：

```text
trigger: SEG-0005=SPK_B
recomputed segments: SEG-0005
recomputed ledger rows: LED-0005
reused ledger rows: LED-0001..LED-0004, LED-0006..LED-0011
byte equality: ledger_v1/v2/common all true
```

15 秒后音频增益修订：

```text
changed ms: [15000, 31199]
recompute frame interval: [1499, 3119]
recomputed ledger rows: LED-0007..LED-0011
reused ledger rows: LED-0001..LED-0006
byte equality: true
```

规则版本提升会复用帧、检测和身份结果，重算异常分类、账本和审计阶段；报告包含新的
`rule_version` 或音频修订内容键。每次更新后，系统在内存中重新执行一次独立全量计算，
并用规范 JSONL 字节与工作区账本逐一比较。

## 六阶段快照

阶段顺序固定为：

1. `frame_features`
2. `turn_detection`
3. `anomaly_classification`
4. `identity_adjudication`
5. `ledger_write`
6. `audit_report`

每个阶段提交 `state/snapshots/<stage>.v000001.json` 和原子替换的
`<stage>.latest.json`。快照含单调递增 `version`、`completed_intervals`、
`completed_ids`、`artifact_keys` 和 `snapshot_key`。阶段未全部完成时不提交新的 latest；
`recover()` 删除 `*.tmp` 与 `pending/*.json`，`resume_plan()` 返回最近连续已提交阶段。

## 错误码与定位

- `E_IDENTITY_DRIFT`：同一语音段出现不同说话人标签，返回首个违规帧。
- `E_SPURIOUS_TURN`：相邻换流点间隔不大于 `min_stable_turn_ms=250`。
- `E_OVERLAP_TURN`：时间戳非严格升序、重复或语音段重叠。
- `E_QUERY_MISMATCH`：实时查询与批量结果不一致。
- `E_INCREMENTAL_MISMATCH`：增量账本与全量重算字节不一致。
- `E_SNAPSHOT_MISMATCH`：快照键与最终账本不一致。
- `E_RULE_MIGRATION_MISMATCH`：两代规则视图业务结论不一致。

`reports/diagnostic.json` 包含 `latest_error_code`、最近业务码和最后异常/账本行的完整
来源追溯链。

## 本地交互应用

运行 `python -m meeting_audit demo --workspace workspace/demo --port 8765` 后打开
`http://127.0.0.1:8765`。页面无 CDN、无外部字体/图片，提供：

- 毫秒/段编号查询；
- 11 个换流点、业务码和说话人表；
- 语音段身份状态和音高；
- v1 JSON 与 v2 SQLite/分片元数据视图；
- 缓存、自检和诊断 JSON；
- 真值修正、规则提升、音频修订和重置按钮；
- 增量重算/复用集合与逐字节校验状态。

## 实际验证记录

验证日期：2026-11-27，环境为 WSL Ubuntu 22.04 兼容工作区、Python 3.10.12。

- 系统 NumPy 2.0.0 + pytest 8.2.0：`14 passed`。
- 临时目标目录安装 `numpy==1.26.4` 后：NumPy `1.26.4`，`14 passed`。
- `python -m compileall -q src scripts`：通过。
- `meeting-audit verify --deep`：`ok=true`，以下步骤全部 `ok=true`：
  `foundation`、`truth_correction`、`rule_promotion`、`audio_revision`、
  `query_repeat`、`snapshot_recovery`、`segmentation_stability`、`final_self_check`。
  完整机器输出保存在 `docs/deep_verification_output.json`。
- `meeting-audit run --workspace workspace/demo`：11 events、11 segments、11 ledger rows、
  `self_check_ok=true`、`foundation_ok=true`；输出保存在 `docs/demo_run_output.json`。
- Web 服务实测 `GET /`、`GET /api/state`、`GET /api/query?ms=12120` 和
  `POST /api/update {"action":"truth"}` 均返回预期结果；真值更新精确重算 `LED-0005`
  并复用其余 10 行。

整个实现不使用 Docker、外部网络、真实音频数据集或 GPU。
