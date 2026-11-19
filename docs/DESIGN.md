# 设计说明：确定性与误差传播

## 1. 确定性边界

合成器使用自实现的 SplitMix64 PRNG，而不是依赖系统时间、哈希随机化或字典遍历。
所有种子由 `derive_seed()` 对稳定字符串和 SHA-256 派生。浮点输出在写出前只做显式
`rint` 和 int16 裁剪。

稳定顺序出现在四个位置：

1. 说话人按 `speaker_id` 升序；
2. 发音单元候选按 `(speaker_id, phoneme_id)` 升序；
3. JSON 使用 `sort_keys=True`、固定分隔符和固定浮点序列化；
4. 快照类别和诊断发现均显式排序。

## 2. 帧几何与真值

默认：

```text
sample_rate = 16000
frame_ms = 20
frame_samples = 320
hop_ms = 20
hop_samples = 320
```

帧数为：

```text
N_frames = 1 + floor((N_samples - frame_samples) / hop_samples)
```

合成器直接使用：

```text
N_samples = (N_frames - 1) * hop_samples + frame_samples
```

因此分析帧不会被隐式补零，也不会丢掉尾部半帧。帧数由 utterance seed 唯一决定：

```text
N_frames(seed) = 24 + SplitMix64(seed).randbelow(16)
```

输入诊断可以仅凭 seed 重算期望 PCM 形状。该机制用于注入
`E_SEED_SHAPE_MISMATCH`，而不是修改源码制造异常。

## 3. 基频候选

对每帧中心化信号计算归一化自相关：

```text
r(l) = sum_t x[t] x[t+l] / sqrt(sum_t x[t]^2 sum_t x[t+l]^2)
```

搜索范围对应 70–400 Hz。谐波信号可能在 `2T` 处略高于 `T`，因此算法先取全局峰值，
再选择与全局峰值相差不超过 0.03 的最小局部峰。这样不会因为浮点噪声选择次谐波周期。

帧内能量使用未加窗平方均值，和 truth 的 `energy_true` 完全同一口径。
频谱使用 Hann 窗，仅用于音色描述，不参与能量真值。

## 4. 音色统计

19 维向量包含：

```text
[0]  mean(log f0)
[1]  std(log f0)
[2]  mean(log(energy + eps))
[3]  std(log(energy + eps))
[4]  mean(zcr)
[5]  std(zcr)
[6]  mean(autocorr peak)
[7]  std(autocorr peak)
[8]  mean(centroid / Nyquist)
[9]  std(centroid / Nyquist)
[10] mean(spectral flatness)
[11] mean(bandwidth / Nyquist)
[12..18] mean(log(band_power / total_power + eps))
```

7 个频带为 `0–500`、`500–1000`、`1000–1500`、`1500–2200`、`2200–3000`、
`3000–4000`、`4000–8000` Hz。频带统计对共振峰位置比单一质心更敏感。

参考嵌入仍进行 L2 归一化，并显式保存 `embedding_norm`。克隆裁决使用原始统计量的
尺度化距离，避免高动态范围分量在点积中压过 f0 和频带信息。

## 5. 对齐候选

8 维帧向量：

```text
f0 position, log-energy position, zcr, autocorr peak,
centroid, bandwidth, flatness, rms position
```

音素原型是固定 8 维表；候选向量为：

```text
candidate = reference_alignment_base + 0.65 * phoneme_prototype
```

候选评分是余弦相似度。选择器按：

```text
(-score, speaker_id, phoneme_id)
```

做完全确定性的排序。

## 6. 精度与量化

阶段一至四的语义不随 tier 改变。`float32` 的输入和每阶段输出均转换为 float32；
`fixed` 每阶段先按 float64 执行同一算子，再量化到整数网格。

舍入定义：

```text
half_even:   numpy.rint
half_away:   sign(x) * floor(|x| + 0.5)
toward_zero: trunc(x)
floor:       floor(x)
ceil:        ceil(x)
```

每阶段记录：

```text
raw_frame_values
raw_candidate_values
frame_values
candidate_values
quantization_residual_max
```

自检重新应用 `quantize(raw, scale, mode)`，要求输出逐元素相等。

## 7. 误差指标

设参考阶段张量为 `R`，观测阶段张量为 `O`，逐分量：

```text
e_abs = |O - R|
e_rel = e_abs / max(|R|, eps)
e_signed = O - R
```

帧级平均绝对偏差为 `e_frame = mean_components(e_abs)`。阶段均值为所有帧
`e_frame` 的均值。95% 区间为：

```text
CI = mean(e_frame) ± 1.96 * std(e_frame, ddof=1) / sqrt(N)
```

放大比：

```text
A_k = mean_abs_k / mean_abs_(k-1)
```

第一阶段的上一级误差不存在，因此定义 `A_1 = 1`。主导来源是 `mean_abs` 最大的阶段。
`evidence_frames` 保存该阶段贡献最大的帧和全部逐分量偏差，用于人工追溯。

候选张量也计算独立偏差统计；阶段合并值取帧张量和候选张量均值的平均。

## 8. 快照一致性

快照校验和：

```text
checksum = SHA256(canonical_json({
  "category": category,
  "version": version,
  "payload": payload
}))
```

提交规则：

1. 版本必须为正整数；
2. 新版本必须大于磁盘当前版本；
3. 相同版本必须有相同校验和，此时幂等返回；
4. 否则返回 `E_SNAPSHOT_ROLLBACK`；
5. 写入完整 wrapper，随后 `os.replace`；
6. 读取时校验类别、版本、JSON 结构和 checksum。

`commit_all` 先对所有类别预检，再进行写入，避免版本冲突导致部分类别已经更新。

## 9. 自检类别

```text
frame_index_set        truth、feature、每档执行帧索引一致
unit_assignment_set    每档 assignment digest 与标签序列一致
error_interval_order   阶段和逐帧区间均为有限值且 low <= high
fixed_recompute        定点档每阶段可由 scale/rounding 重算
clone_decision_trace   top-k 裁决的 detail_digest 和帧集合可追溯
```

任何失败都转换为 `E_INCONSISTENT`，并在 finding 中给出 `category`。
