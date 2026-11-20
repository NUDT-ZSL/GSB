# 技术说明：设计、归约语义与验证结果

## 1. 确定性合成与观测口径

- 合成器 `seraph_core/data/synth.py` 以 `SeedSequence([seed, ...path])`
  派生每个话轮、每帧漂移的独立随机源，路径参数显式入熵，新增对话不会
  改变既有对话的字节。
- 话轮由基频脉冲、二次谐波、共振峰正弦与小幅抖动叠加，经起音/释音
  升余弦包络与峰值归一后量化为 int16 PCM（16kHz、单声道）。
- 逐帧真值字段固定为
  `frame_index, speaker_id, valence, arousal, is_speech`；帧中心落在话轮
  区间内才标记为语音，情感值由说话人基线、情绪增量与确定性漂移裁剪到
  `[-1, 1]`。观测口径即该真值文件的 valence/arousal，段目标为段内语音
  帧真值的算术均值。

## 2. 分帧、切段与池化

- 帧边界由样本索引精确确定：`frame_length = sr*frame_ms/1000`，
  `hop_length = sr*hop_ms/1000`，配置校验拒绝非整样本对齐。
- 帧特征 5 维：短时能量、过零率、自相关 F0 候选、平均绝对值、一阶差分
  RMS；F0 在 70–300Hz 候选滞后区间取首个最大相关峰，平局由 `argmax`
  的首索引规则确定。
- 切段以真值 `is_speech` 为主通道，连续静音帧数达到
  `min_silence_frames` 才切段，短静音并入同段。
- 段向量 = 5 维帧特征的 mean/std/max（15 维）+ 段帧数与语音占比
  （2 维），共 17 维；标准化统计量在全量段上一次性 float64 计算，
  标准差落入次正规数区间时按配置替换为 1.0。

## 3. 三档精度的统一算子语义

三档后端 `seraph_core/engine/precision.py` 执行完全相同的算子序列
（matvec → 分块点积 → 从左到右顺序累加 → 标量步长更新），仅舍入行为
不同：

- `float64`：参考实现，NumPy float64。
- `float32`：每个乘积与每次累加显式经过 float32；`half_even` 走原生
  float32，`half_up`/`truncate` 在 float32 ULP 粒度模拟舍入，并按
  `subnormal_protection` 处理次正规数。
- `fixed`：数值乘 `fixed_scale` 后按配置模式舍入为 int64，int64 内
  精确累加，再除以缩放因子还原；点积除以 `scale^2`。缩放因子上限
  `2^31`，非法值在配置层拒绝。

分块计划 `chunk_plan` 只产生确定性连续区间，梯度按
`reduction_order="sequential"` 对各块偏导从左到右归约，因此不同对话
时长造成的块边界差异只会改变累加顺序，不会改变算法路径。

## 4. 分块/整段对照与逐级误差归因

- 参考真值固定为 **float64、整段（chunk_size=0）、默认缩放** 的同数据
  训练结果。
- 需求 3 的归约顺序对照使用同精度配对：float32 整段 half-even 对照
  float32 分块，逐轮输出损失最大绝对偏差、收敛状态分叉及首次分叉轮次，
  最终输出逐参数偏差谱；同时保留 float64 分块对照以证明顺序效应在
  float64 下不可见。
- 五级误差隔离（`diag/error_spectrum.py`）均与同精度基线成对：
  特征缩放 2x、学习率缩放 0.5x、定点量化对 float64 整段；分块累加、
  舍入模式（truncate 对 half-even）对 float32 整段。每级输出绝对偏差
  （max/mean/p50/p90/p95/p99）、相对偏差、误差放大比（级均值偏差 /
  基线权重均值尺度）与共同参考空间下的最终损失差。
- 主导来源取最大绝对偏差最高的一级，`traceability` 给出每级到偏差
  明细条目的回溯键，保证结论可复核。

## 5. 四阶段快照与恢复

- 阶段：`framing → pooling → training → summary`。每阶段快照信封含
  单调版本号、schema 版本、配置指纹、payload 与 npz 的 SHA-256、总
  校验和；写入走临时文件 + `os.replace` 原子提交，`complete.json`
  只在阶段全部完成后落盘。
- 训练阶段每个 epoch 提交一次进度快照（含逐轮权重、损失、保护轮次、
  分块计划、降级记录）。强制终止钩子在 epoch 快照提交之后抛
  `ForceTermination`，因此未提交的块梯度与轮次状态只存在于内存，重启
  天然从最后已提交轮次的起点重建。
- 重启时 schema 版本或配置指纹不一致直接返回
  `E_SNAPSHOT_CORRUPT`，要求整目录替换（`reset_stage`），不做增量
  修补；损坏字节、缺失 npz、校验和不符走同一错误码。
- 阶段提交使用 `O_CREAT|O_EXCL` 非阻塞锁，冲突立即返回
  `E_STAGE_BUSY`。

## 6. 收敛门限与资源控制

- `strict`：改善量 `< tolerance` 即收敛；`tolerance_band`：改善量
  `< tolerance + band` 即收敛，带内结果在重复运行下不会被舍入翻转；
  次正规数保护开启时，小于 `8*tiny` 的改善量归零。
- 资源上限：对话数、驻留段数、梯度内存估算。超限时先把分块减半直到
  `min_chunk_size` 并记录 `chunk_size_downgrade` 降级类别，仍超限才
  返回 `E_RESOURCE_LIMIT`。

## 7. 自检口径

`diag/consistency.py` 校验：

1. 分帧/池化/汇总的 `dialog_id` 集合完全一致；
2. 每轮损失可用当轮权重与特征独立复算（容差 1e-12）；
3. 分块计划可由行数与块大小确定性重算；
4. 分块/整段偏差谱可重算一致（容差 1e-15）；
5. 受保护轮次均有执行或跳过记录、跳过轮次必有保护快照。

任一失败返回 `E_INCONSISTENT` 并在 `item` 中指明来源条目。

## 8. 实测结果（本机 Python 3.10.12 / NumPy 1.26.4）

执行 `python -m pytest`：**61 passed**。

默认 6 对话语料、40 轮配置的 `reports/full_run_report.json` 关键数值：

| 误差级别 | 最大绝对偏差 | 相对偏差 |
| --- | ---: | ---: |
| feature_scaling | 3.841e-02 | 2.911e-01 |
| chunk_accumulation | 7.451e-09 | 4.057e-08 |
| fixed_quantization | 1.415e-01 | 9.366e-01 |
| rounding_mode | 5.960e-08 | 3.713e-07 |
| lr_scaling | 5.911e-02 | 3.520e-01 |

主导来源：`fixed_quantization`，与定点缩放整数的舍入量级一致；分块
累加在 float32 下仅引入 1e-9 级顺序偏差，float64 下为 0。续算场景
（第 3 轮后终止）`weights_byte_identical=true`、
`loss_trajectory_identical=true`；非法采样率返回
`E_CONFIG_INVALID(field=sample_rate)`。

## 9. 复现实验命令

```bash
python -m scripts.generate_data
python -m scripts.demo --scenario all
python -m pytest
```
