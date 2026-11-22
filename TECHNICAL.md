# TTS 本地确定性合成应用 — 技术说明

面向语音合成的本地专业应用，核心业务问题：**合成配置的增量更新与依赖失效**。
全程不使用 Docker / 外部服务 / 网络，仅依赖 Python 3.10+ 与 NumPy。

## 项目结构

```
tts_app/
  errors.py       业务错误码（E_*）与 TtsError
  config.py       v1/v2/v3 配置读取器（按版本头/字段集分派）
  migrate.py      v1<->v2/v3 双向迁移器（补全来源 + 丢弃明细）
  adjudicate.py   跨方言裁决（E_SEMANTIC_DRIFT）
  synth.py        确定性合成原语（说话人参数/音素/声学/声码器/逐帧对齐）
  dag.py          四阶段计算图引擎 + 缓存键构造 + 资源上限
  cache.py        内容寻址缓存（键组成可反查）
  lineage.py      谱系追溯与过期拒绝（E_LINEAGE_BROKEN）
  snapshot.py     快照版本化整体替换 + 并发提交控制
  app.py          本地可交互专业应用（三视图）
scripts/gen_samples.py   确定性样本生成（说话人/任务清单/逐帧真值）
configs/                 v1/v2/v3 配置样例（含补全与丢弃场景）
tests/                   33 项 pytest 测试
demo.py                  可重复演示脚本
```

## 关键设计

### 确定性合成（需求 1）
- 图拓扑：`normalize -> phonemes -> acoustic -> vocoder -> align`，节点连接由配置字典驱动。
- 全部随机源来自 `stable_seed(seed, utterance_id, ...)` 派生的 `numpy.RandomState`，
  禁止系统时间与未声明随机源；同 utterance_id + seed + 配置 => PCM 逐字节一致。
- 声码器：谐波源 + 共振峰整形 + 能量包络，16kHz 单声道 int16 PCM；`fp16` 精度档
  通过 float16 量化影响键与产物。

### 配置读取（需求 2）
- v1 扁平位置行 `V1|spk|style|prosody|precision|vocoder`；v2 键值行 `V2|k=v|...`；v3 嵌套 JSON。
- 未知版本 -> `E_VERSION_UNSUPPORTED`；头部截断 / 竖线字段数不符 / 必填缺失 / 数值不可解析
  -> `E_FORMAT_CORRUPT`，details 携带 `line` 与 `field`；结果保留 `source_version`。

### 双向迁移（需求 3）
- 升级补全 `style.tag / prosody.scale / precision / prosody.duration_scale`，
  逐项记录于 `filled_defaults`（含 `source: default:*`）。
- 降级对 `style.inherit`（drop）与 `prosody.duration_curve`（drop/flatten）按显式
  `rules` 处理并输出 `dropped` 明细；未声明规则直接报 `E_SEMANTIC_DRIFT`，禁止静默默认。
- 迁移记录携带 `source_version` 与 `target_version`。

### 跨方言裁决（需求 4）
- 三种方言统一升级到 v3 规范化视图逐项对比；不一致或 v1 往返不可还原时
  抛 `E_SEMANTIC_DRIFT` 并列出 `unrecoverable` 配置项。
- `cross_dialect_pcm_check` 验证三种书写产出相同音素、声学特征与逐字节相同 PCM。

### 内容寻址缓存（需求 5）
- 键 = sha256(规范化 JSON 键组成)；组成含 text_hash、phoneme_digest、
  speaker_embed_version、speaker_params_hash、style、prosody、precision、
  vocoder_version、vocoder_params 与上游节点哈希。
- 键组件按节点作用域**前向流动**（上游键不含下游版本），从而增量修改仅使受影响
  后缀失效；最终节点键覆盖全部组件。`<key>.meta` 支持键组成反查。

### 增量失效传播（需求 6）
- 修改说话人 f0/共振峰 -> 该说话人 utterance 的 `acoustic/vocoder/align` 重算，
  `normalize/phonemes` 复用；改风格/韵律 -> 对应 utterance 后缀重算；
  声码器版本提升 -> 全体 `vocoder/align` 重算。
- `run_all` 输出 `recomputed_nodes` / `reused_nodes` / `affected_utterances`，
  测试保证增量结果与从零全量合成逐字节一致。

### 谱系追溯（需求 7）
- 每条 utterance 写 `<utt>.lineage.json`：缓存键、键组成、配置版本、节点路径、PCM 哈希。
- `verify_lineage`：依赖版本（说话人参数/风格/韵律/精度/声码器版本等）已变化而产物
  仍来自旧键 -> `E_LINEAGE_BROKEN` 并指出 `utterance_id` 与 `changed_dependencies`；
  追溯链缺失（`lineage_missing`）或重放不符（`replay_mismatch`/`replay_missing`）同码。

### 快照 / 并发 / 资源上限（需求 8）
- 快照携带单调递增版本号 + 内容校验和，提交即整体替换（旧数据文件清除）；
  版本回退或同版本覆盖 -> `E_SNAPSHOT_ROLLBACK`。
- `StageLock` 非阻塞互斥，冲突提交立即获得 `E_STAGE_BUSY`。
- `Limits`：单次重算 utterance 数 / 缓存条目总量 / 并行节点数；严格模式超限
  -> `E_RESOURCE_LIMIT`，降级模式标记类别（`utterance_batch_split` /
  `parallel_clamped` / `cache_evict`）。
- 应用 `python -m tts_app`：计算图拓扑 / 缓存命中与重算 / 快照与失效覆盖三视图切换，
  展示快照版本与最近业务错误码，支持触发合成演示与快照提交。

## 运行方式

```bash
python3 scripts/gen_samples.py data   # 生成确定性样本
python3 demo.py                       # 可重复演示（17 项检查）
python3 -m pytest tests -q            # 33 项测试
python3 -m tts_app workspace          # 交互式应用
```

## 实际验证结果（2026-11-23 本机运行）

- `python3 -m pytest tests -q`：**33 passed**。
- `python3 demo.py`：**17 项检查全部通过**，要点：
  - 确定性：6 条 utterance 两次独立运行 PCM 哈希一致；
  - 迁移：v1 稀疏配置补全 3 项默认值并记录来源；v3 漂移配置降级输出
    `style.inherit=drop`、`duration_curve=flatten(1.05)` 明细；
  - 裁决：等价三方言产出相同 PCM（`0d38ee975660c977...`）；漂移场景返回
    `E_SEMANTIC_DRIFT`，unrecoverable=`style.inherit / prosody.duration_curve / prosody.duration_scale`；
  - 增量：修改 spk_alpha 参数仅影响 utt_001/utt_003，复用 20 节点、重算 10 节点，
    与从零全量合成逐字节一致；声码器版本提升影响全部 6 条；
  - 谱系：utt_003 重放一致；依赖变化后返回 `E_LINEAGE_BROKEN`（变化依赖
    `speaker_params_hash`）；
  - 快照：v1->v2 整体替换且校验和有效，回退返回 `E_SNAPSHOT_ROLLBACK`，
    并发提交返回 `E_STAGE_BUSY`，超限返回 `E_RESOURCE_LIMIT`。
- 报告落盘：`workspace_demo/demo_report.json`。
