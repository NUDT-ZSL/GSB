# 实际验证记录

## 环境

- 执行环境：Linux（当前容器）
- 解释器：Python 3.10.12
- 依赖：NumPy 1.26.4
- 推荐目标：Python >= 3.11（Windows 官方安装包）
- 包声明：Python >= 3.10（兼容当前验证容器）
- 运行模式：纯本地、CPU、无网络、无容器

当前容器未提供 Python 3.11 解释器，因此实际命令在 3.10.12 上执行；实现未使用 3.11 独有语法，目标环境为 Python 3.11。

## 自动化测试

```text
python -m unittest discover -s tests -v

Ran 32 tests in 3.715s
OK
```

覆盖项：

- 数据集合同、16 kHz、整帧样本边界与逐帧真值。
- 两次独立合成的 PCM、特征与对齐摘要逐字节一致。
- 受控音素越界错误包含 utterance_id 与 frame_index。
- 同分排序使用 speaker_id、phoneme_id。
- 内容哈希幂等、相同 ref_id 不同内容触发重估、未变化说话人摘要字节稳定。
- 漂移可由新旧嵌入、参考版本独立复算。
- 三档精度的对齐摘要一致；非法精度/缩放/舍入字段被结构化拒绝。
- 四级误差谱均由明细重算；区间逆序和明细篡改返回 `E_ERRPROP_INCONSISTENT`。
- 自适应选择确定性、规模切换与近似路径全音素覆盖。
- 门限附近跨精度判定稳定（`strict` 与 `tolerance_band` 共 72 次探针）、次正规零范数保护、跨路径不可比对返回 `E_PATH_DRIFT`。
- 快照三区校验和、前向提交、过期提交/回退拒绝。
- 五类注入业务码与降级类别。
- 自检首差异定位。
- HTTP 健康、状态、精度切换、注入与自检接口。

## 端到端演示

```text
python app.py demo
overall:               PASS
dataset:               4 speakers, 12 utterances, 460 frames
alignment invariant:   True
dominant fixed source: feature_scaling
drift S1:              0.015616978754 (recompute=True)
path agreement:        1.0
snapshot:              v3 verified=True
injections:            sample_rate=E_SAMPLE_RATE,
                       seed_shape=E_SEED_SHAPE,
                       ref_missing=E_REF_MISSING,
                       duplicate_utterance=E_DUPLICATE_UTTERANCE,
                       phoneme_out_of_range=E_PHONEME_OUT_OF_RANGE
```

关键数值：

| 项目 | 结果 |
|---|---|
| 数据集摘要 | `0428226cca72a678c066b3219c44f132751c0fdd26bdb04fd55251054241b8ab` |
| float64 / float32 / fixed 对齐摘要 | 完全一致 |
| S1 归一化 L2 漂移 | `0.015616978754` |
| S1 相对漂移 | `0.015616978754` |
| S1 余弦距离 | `0.000121945013` |
| 漂移独立复算 | `True` |
| S2/S3/S4 未变化摘要字节稳定 | `True` |
| 重算/复用集合交集 | `[]` |
| 重算/复用集合并集覆盖 460 帧 | `True` |
| 精确/近似相似度一致率 | `1.0` |
| 精确/近似门限判定一致率 | `1.0` |
| 对齐失配率 | `0.130434782609` |
| 置信偏差 p95 | `0.0029234278` |
| fixed 误差谱复核 | 460 帧全部通过 |
| fixed 主导来源 | `feature_scaling` |
| float32 主导来源 | `similarity_accumulation` |
| 快照整体校验 | `True` |
| 回退尝试 | `E_SNAPSHOT_ROLLBACK` |
| 五类注入 | 均返回预期业务码且保留活动状态 |

详细报告：

- `artifacts/reports/demo_report.json`
- `artifacts/reports/demo_report.md`
- `artifacts/reports/selfcheck.json`

`demo` 命令完成后的演示快照为 v3；随后单独运行 `python app.py check` 时，进程会从初始参考片段重建内存状态，并按整体替换策略前向同步到 v4，自检仍为 `ok=true`、`first_difference=null`。版本从不会回退。

## 可重复性

演示内部会启动两个独立临时工作区，分别合成并计算：

- 数据集摘要
- float64 特征摘要
- 对齐摘要
- 自适应决策摘要
- fixed 误差谱摘要

五项均一致，证明相同 seed、相同配置下核心结果可重复。
