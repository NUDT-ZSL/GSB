# VocalForge Local

面向音色克隆与发音建模的本地可交互专业应用。项目不依赖云端、GPU、容器、账号或外部数据集；多说话人语音、参考音色片段、逐帧真值和样本清单全部由固定种子在程序内确定性合成。

## 快速开始

推荐环境为 Windows + Python 3.11，依赖仅 `numpy==1.26.4`。为兼容当前验证容器，包声明最低版本为 Python 3.10；目标部署仍建议 3.11。

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python app.py demo
python app.py serve
```

浏览器打开 `http://127.0.0.1:8765/`。Linux/macOS 使用对应的 `python3.11 -m venv .venv` 即可。服务只监听 `127.0.0.1`，不会访问线上服务。

常用命令：

```text
python app.py demo        # 完整可重复演示，生成 JSON/Markdown 验证报告
python app.py serve       # 启动本地交互仪表盘
python app.py check       # 仅运行跨级一致性自检
python -m unittest discover -s tests -v
```

工作目录必须可写。缓存、快照、诊断报告和样本输出位于 `artifacts/`：

```text
artifacts/
  samples/
    manifest.jsonl          # utterance_id, speaker_id, ref_id, sample_rate,
                            # duration_ms, phoneme_count, clone_seed
    truth.jsonl             # frame_index, speaker_id, f0_true, energy_true, phoneme_id
    references.jsonl        # 参考片段清单、内容哈希与版本
    pcm/*.pcm               # 16 kHz 单声道 int16 little-endian PCM
    references/*.pcm
  cache/
    current_snapshot.json   # 当前完整快照
    snapshots/v*.json       # 只追加的前向快照归档
  reports/
    demo_report.json
    demo_report.md
    selfcheck.json
```

## 应用能力

- 确定性合成 4 个说话人、12 条 utterance、460 帧、8 个受控音素的 16 kHz 单声道 PCM。
- 20 ms 帧长、10 ms 帧移；帧边界直接由样本索引计算，特征包含短时能量、过零率、F0 候选、谱质心与四频带能量。
- 参考音色嵌入由参考片段帧统计量加权估计；注册追加以 PCM 内容 SHA-256 判断变化，只重估变化说话人。
- float64 / float32 / 定点缩放整数三档精度共享算子语义；对齐标签使用规范化决策层，不随精度档漂移。
- 四级误差传播：特征缩放、嵌入归一化、对数变换、相似度累加；输出绝对/相对偏差、分位数、放大比和主导来源。
- 自适应精确/近似路径选择；近似路径的候选并集强制覆盖全部受控音素。
- 音色相似度门限支持 `strict` 与 `tolerance_band`，包含决策量化、top-k 稳定排序和次正规数保护。
- 快照三个统计区各自携带单调版本号和内容校验和，整体原子替换；回退请求返回 `E_SNAPSHOT_ROLLBACK`。
- 五类业务错误注入与显式降级类别；跨级一致性自检定位首个差异位置。

## 交互界面

仪表盘包含九个工作区：

1. 运行总览：数据规模、链路健康、算法取舍、最近错误。
2. 音色嵌入：每个说话人的嵌入向量、摘要、参考版本、内容哈希；可追加注册样本并查看漂移。
3. 逐帧对齐：按 utterance 与精度档查看帧边界、对齐标签、真值标签、分数和边距。
4. 精度档：切换 float64 / float32 / fixed，查看三档摘要和标签不变量。
5. 误差谱：按目标精度档查看四级统计、误差放大链、主导来源和明细计数。
6. 自适应路径：规模观测、阈值、候选覆盖、比较次数与跨路径裁决。
7. 相似度裁决：在线修改门限、top-k、策略和容差；查看稳定判定与置信偏差谱。
8. 快照与诊断：快照版本/校验和、错误注入、降级链路、回退拒绝与错误历史。
9. 一致性自检：检查 frame_index 集合、误差区间、定点重算、集合覆盖、门限和 top-k 稳定性。

## 核心设计

### 确定性与帧语义

- 合成 RNG 为 `numpy.random.default_rng`，所有 utterance/reference 使用由数据集 seed 派生的独立种子。
- `frame_len = round(16000 × 20 / 1000) = 320` 样本，`hop = round(16000 × 10 / 1000) = 160` 样本。
- 第 `i` 帧固定为 `[i × hop, i × hop + frame_len)`，不依赖静音检测或流式缓冲。
- 对齐排序键为 `(-score, speaker_id, phoneme_id)`，因此同分时先 speaker_id、再 phoneme_id。
- 所有摘要使用稳定 JSON 排序和 SHA-256；测试比较两次独立合成的特征、标签与决策摘要。

### 参考嵌入与漂移

每条参考片段提取帧级 8 维音色向量，汇总为 12 维嵌入（均值 + 前四维标准差）。同一说话人的多个参考片段按帧数加权。

```text
drift_l2          = ||new_vector - old_vector||₂
drift_normalized  = ||new_normalized - old_normalized||₂
drift_relative    = drift_normalized / max(||old_normalized||₂, 1e-12)
cosine_distance   = 1 - cos(old_normalized, new_normalized)
```

漂移摘要还绑定 old/new 参考版本、片段内容哈希和 `embedding-drift-v1` 公式版本，可仅由新旧嵌入与参考版本独立复算。

### 精度档

| profile | 转换方式 | 附加参数 |
|---|---|---|
| `float64` | 原生双精度 | — |
| `float32` | 每步转 float32 后回到 float64 存储/序列化 | — |
| `fixed` | `clip(round(x × scale), -2^62, 2^62) / scale` | 正整数 `fixed_scale`、显式 `rounding_mode` |

支持的舍入模式：`nearest_even`、`half_up`、`truncate`、`floor`。未知精度档、非正缩放因子、非法舍入模式均返回 `E_PROFILE_INVALID` 并给出字段名。

对齐标签始终来自 float64 规范化特征，因此精度档只影响数值评分和误差谱，不改变对齐标签或音素归属。

### 误差传播

每个快照以 float64 结果为参考真值，逐帧 `f0_true` / `energy_true` 作为观测归一化口径，分别计算：

1. `feature_scaling`
2. `embedding_normalization`
3. `log_transform`
4. `similarity_accumulation`

每级保存逐帧 `reference`、`observed`、`abs_dev`、`rel_dev` 明细，重建 p50/p90/p95/p99/max/mean、放大比和连续误差区间。复核器从明细重算所有统计；定点档还用 `fixed_scale` 与 `rounding_mode` 对 canonical 值逐帧重算实际定点值。任何区间逆序、统计不一致、摘要不一致或无法重算都返回 `E_ERRPROP_INCONSISTENT`。

### 自适应路径与一致性

选择器依据说话人数、总帧数、受控音素数、精确路径驻留特征内存估计和内存余量做纯函数决策：

- 精确：细帧移、全量音素比较、精确 L2 归一化。
- 近似：粗帧移、候选剪枝、分块 L2 估计；候选并集与受控音素集合强制相等，缺失时 `E_INCONSISTENT`。

跨路径裁决输出相似度一致率、门限判定一致率、对齐失配率、标签偏移和置信偏差谱。无共同 utterance、非有限相似度或一致率低于策略门限时返回 `E_PATH_DRIFT` 并说明 `source`。

### 门限稳定性

- 相似度与门限都先量化到配置的 `decision_quantum`。
- `strict` 在量化决策空间比较。
- `tolerance_band` 对量化距离落在容差带内的样本采用稳定接受策略。
- L2 归一化前将绝对值小于 `subnormal_floor` 的分量置零，零范数返回零向量。
- top-k 使用 `(-quantized_similarity, utterance_id, speaker_id)` 排序。
- 自检对每条 utterance 将门限设为 float64 分数的量化值，比较三档判定结果。

### 快照与错误码

每次提交构造全新快照对象，包含 `embedding_stats`、`precision_config`、`error_spectrum_stats` 三个区。每区：

```text
version  单调递增；内容不变则保持原版本
checksum SHA-256(canonical section payload)
```

整体快照另带 `version`、`parent_version`、`dataset_digest` 和总校验和；写入临时文件后原子替换 `current_snapshot.json`。仅接受前向版本，回退或过期 base version 返回 `E_SNAPSHOT_ROLLBACK`。

| 业务码 | 触发条件 | 降级类别 |
|---|---|---|
| `E_PHONEME_OUT_OF_RANGE` | 标签/真值不在受控音素集合 | `alignment_restricted` |
| `E_REF_MISSING` | 参考片段或 ref_id 缺失 | `embedding_reused` |
| `E_PROFILE_INVALID` | 精度档或定点参数非法 | `precision_fallback` |
| `E_ERRPROP_INCONSISTENT` | 误差区间/明细/统计不可重算 | `diagnostics_degraded` |
| `E_PATH_DRIFT` | 精确与近似路径不可比对 | `path_arbitration_fallback` |
| `E_SNAPSHOT_ROLLBACK` | 回退或过期快照写入 | `snapshot_guard` |
| `E_SAMPLE_RATE` | 采样率不是 16000 | `input_rejected` |
| `E_SEED_SHAPE` | seed 或片段形状不匹配 | `registration_skipped` |
| `E_DUPLICATE_UTTERANCE` | utterance_id 重复 | `duplicate_suppressed` |
| `E_INCONSISTENT` | 跨级自检失败 | `diagnostics_blocked` |

所有错误响应包含 `code`、`message`、`stage`、可选 `field`、`details`、`degradation`；注入结果另带 `injection_stage` 与 `active_state_preserved`。

## HTTP API

```text
GET  /api/health
GET  /api/state
GET  /api/alignment?profile=fixed&utterance_id=utt_s01_001
GET  /api/precision
GET  /api/error-spectrum?profile=float32
GET  /api/similarity?profile=float64
GET  /api/snapshot
GET  /api/consistency

POST /api/registration          {"speaker_id": 1}
POST /api/precision             {"profile": "float32"}
POST /api/similarity-config     {"threshold": 0.985, "top_k": 5,
                                 "strategy": "tolerance_band", "tolerance": 0.00002}
POST /api/inject                {"stage": "phoneme_out_of_range"}
POST /api/rollback              {"target_version": 1}
```

## 测试与验收

测试覆盖合成/帧边界/确定性、参考哈希与局部重估、漂移独立复算、精度档错误、误差谱重算、自适应取舍与覆盖、门限稳定性、次正规保护、跨路径漂移、快照回退、五类注入、自检首差异和 HTTP API。

最近一次实际验证结果见 `VALIDATION.md` 和 `artifacts/reports/demo_report.*`。当前执行容器仅提供 Python 3.10.12，代码同时兼容 Python 3.11；项目目标部署环境为 Python 3.11。
