# 语音情感与对话理解组件 — 架构与算法说明

## 1. 设计目标与约束

- 仅依赖 Python 3.10+ 标准库与 NumPy；无外部音频、模型权重或线上服务。
- 全链路确定性：固定种子、确定性随机数 (`numpy.random.Generator`)、无随机初始化的
  聚类/回归、规范化 JSON（`sort_keys`、固定换行、固定小数位），同输入同配置逐字节复现。
- 数据规模变化时在“精细路径”与“逼近路径”之间自适应取舍，并显式记录依据与降级类别。
- 所有预期失败以稳定业务码返回，CLI 以退出码 2 表达，并把业务码写入 JSON 报告。

## 2. 模块组成

| 模块 | 职责 |
| --- | --- |
| `synth.py` | 固定种子合成单声道 16kHz PCM、逐帧真值 CSV、JSONL 清单 |
| `framing.py` | 样本精确的帧边界（毫秒→整数样本索引、左对齐分帧、流式迭代） |
| `features.py` | 帧级声学特征（精细 8 维 / 逼近 4 维），支持锚点降采样重建 |
| `diarization.py` | 注册段统计、确定性段级聚类、speaker 升序破平与切换时间轴 |
| `emotion.py` | 岭回归情感打分、置信区间、按时长加权的对话级聚合 |
| `selector.py` | 规模观测、路径决策、显式优先级冲突裁决与降级标记 |
| `consistency.py` | 跨阶段帧集合/分段拼接/聚合可重算一致性自检 |
| `snapshot.py` | 单调版本号 + SHA-256 校验和的路径快照，整体替换语义 |
| `dataio.py` | 清单/PCM/真值加载与输入校验链路 |
| `pipeline.py` | 端到端编排与确定性 JSON 报告 |
| `cli.py` | `generate` / `run` / `verify-snapshot` 命令行 |

## 3. 两条算法路径

### 精细路径 `fine`

- 8 维特征：energy、log-energy、zcr、centroid、zc-peak、rolloff、flux、f0。
- 精确帧网格（`frame_stride=1`），逐帧求值。
- 说话人分离：注册段原型 + 受约束（每集群非空）的 Lloyd 迭代，固定迭代上限。
- 情感回归：设计矩阵含截距的岭回归，固定步数坐标下降求解，无随机初始化。

### 逼近路径 `approximate`

- 4 维特征：energy、log-energy、zcr、f0（保留最具说话人区分度的基频）。
- 默认仍在完整 GT 对齐帧网格上输出；仅当驻留单元超预算时引入 `frame_stride>=2`，
  对锚点求值后按最近锚点重建，**帧边界与帧索引集合永不改变**。
- 说话人分离：注册原型一次精炼 + 单遍受约束最近质心，比较次数为线性 `O(runs*k)`。
- 情感回归：log-energy 单变量闭式岭回归，由流式充分统计量即可计算。

## 4. 规模观测与冲突裁决

观测维度：对话数、总帧数、最大说话人数、特征维度、单对话最大帧数。
全部落在 `PathProfile` 阈值内且资源投影不超限才走精细路径，否则走逼近路径。

显式优先级（高到低）：

1. `P1_hard_resource_budget`：驻留帧、分离比较次数、回归中间量硬预算优先；
2. `P2_speaker_assignment_correctness`：保证 speaker 编号契约与无凭空说话人；
3. `P3_frame_boundary_integrity`：任何降级都不得改变帧边界与帧索引集合；
4. `P4_emotion_scoring_precision`：在上述约束满足后才追求情感打分精度。

`on_exceed=error` 时硬预算超限直接 `E_RESOURCE_LIMIT`；`degrade` 时切换逼近路径并在
报告 `path_decision.degradation` 标注降级类别（如 `DIMENSIONALITY_REDUCTION`、
`FRAME_STRIDE_APPROXIMATION`、`SINGLE_PASS_SEPARATION`、`CLOSED_FORM_REGRESSION`）。

## 5. 确定性与可复现性保证

- 合成器以 `(seed, dialog_id)` 派生独立 `SeedSequence`，重写同名文件字节一致。
- 聚类平局按集群 id 升序；speaker 映射按注册时间序，碰撞时按稳健 f0 均值升序对齐。
- 浮点聚合按固定段顺序求和；报告数值统一四舍五入到 10 位小数并以排序键序列化。
- 已验证：同输入两次运行的 JSON 报告 `cmp` 逐字节相同。

## 6. 业务错误码

| 码 | 触发场景 |
| --- | --- |
| `E_CONFIG_INVALID` | 采样率非 16k、frame_ms 非法、hop>frame、speaker 上限非正、未知配置字段等 |
| `E_FRAME_MISMATCH` | 特征帧数与真值/清单帧数不一致（携带 dialog_id） |
| `E_SPEAKER_MISMATCH` | 凭空说话人、集群数与注册说话人数不符 |
| `E_SPEAKER_UNASSIGNED` | 注册说话人整段未指派或注册运行不足 |
| `E_INTERVAL_INVALID` | 置信区间下界大于上界 |
| `E_RESOURCE_LIMIT` | 驻留帧/比较次数/回归项超预算且策略为 error |
| `E_INCONSISTENT` | 帧集合、分段拼接、聚合重算等跨阶段不一致 |
| `E_SNAPSHOT_CORRUPT` | 快照 JSON 损坏、校验和不符、版本回退 |
| `E_DUPLICATE_DIALOG` | 清单出现重复 dialog_id |
| `E_IO` | 本地文件缺失等 IO 问题 |

## 7. 快照版本化

`path_snapshot.json` 含 `version`（单调递增正整数）、`checksum`（对 `content` 规范化
JSON 的 SHA-256）、`content`（配置、路径定义、决策）。保存时若新版本号低于现存版本即
拒绝；正常升级先清空快照目录再整体写入，保证不存在新旧定义混杂。加载时先重算校验和，
不符即 `E_SNAPSHOT_CORRUPT`。

## 8. 验证结果摘要

在本地合成集（3 个 2 说话人短对话 + 6 个 4 说话人长对话）上：

- 单元测试 27 项全部通过。
- 精细/逼近两份报告分别重跑均逐字节一致。
- 说话人帧级准确率（相对真值）：精细小对话约 0.93、精细 4 说话人约 0.80；
  逼近路径在显著更低比较次数下小对话约 0.82、4 说话人约 0.57，体现精度/预算取舍。
- 全部业务码与快照损坏路径均有可复现的验证用例或演示。
