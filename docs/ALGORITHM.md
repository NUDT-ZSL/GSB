# 算法与数据契约

## 1. 确定性样本和热图

样本种子：

```text
sample_seed   = base_seed + image_index * 1,000,003
heatmap_seed  = base_seed + image_index * 130,363 + 17
```

每具骨架包含 K 个关键点。第 `s` 具骨架、第 `k` 个关键点占用热图通道：

```text
channel = s * K + k
```

真值与目标热图均按二维高斯生成：

```text
H(x, y) = amplitude * exp(-((x-cx)^2 + (y-cy)^2) / (2*sigma^2))
amplitude = 0.72 + 0.28 * confidence
```

真值热图位于真值坐标；目标热图带确定性亚像素抖动，抖动由 `heatmap_seed` 驱动。推理只消费目标热图。落盘数组形状为 `[2, C, H, W]`，第 0 层是真值热图，第 1 层是目标热图。清单元数据中的 `heatmap_sha256` 是 dtype、shape 和原始字节的 SHA-256；读取时会从 `heatmap_seed` 重建并核对，落盘文件也必须与重建哈希一致。

## 2. 峰值解码

### 精确路径

对每个通道检查完整像素网格的 8 邻域严格局部极大值。峰值排序：

```text
强度降序 -> keypoint_index 升序 -> y 像素升序 -> x 像素升序
```

因此强度相同时由关键点索引和像素坐标决定，最终 JSON 顺序逐字节稳定。

水平、垂直亚像素偏移均使用峰值像素及左右/上下邻居的一维二次插值：

```text
offset = clamp(0.5 * (left - right) / (left - 2*center + right), -0.5, 0.5)
```

边界规则：

- 峰值在画布边界时，该轴偏移固定为 `0`；
- 分母绝对值小于 `1e-12` 时偏移为 `0`；
- 最终坐标再裁剪到 `[0, W-1] x [0, H-1]`；
- 输出偏移为实际采用的 `final_coordinate - pixel_coordinate`。

比较次数上界：

```text
channels * H * W * 8 + refinement_operations
```

### 近似路径

每个通道先做 `B x B` max-pooling，B 默认 3，再在粗网格上做 8 邻域峰值检测。峰值坐标为获胜粗块的中心，亚像素偏移固定为 `(0, 0)`，量化半径由 `approx_quantization_px` 声明。

比较次数：

```text
channels * H * W                         # pooling scans
+ channels * ceil(H/B) * ceil(W/B) * 8  # coarse peak checks
```

精确与近似路径始终覆盖相同通道集合和关键点索引，近似路径不丢弃通道。

## 3. 分组和骨架拼接

每条通道选择强度最高的峰值，然后按：

```text
(skeleton_id, source)
```

归并为姿态实例。

规则：

- 同一峰值 ID 被两个实例占用时返回 `E_KEYPOINT_CONFLICT`；
- 实例必须覆盖全部 K 个关键点；
- 实例内按样本显式 `edges` 构建图，必须只有 1 个连通分量；
- 任一边距离超过 `max_edge_distance_px` 时返回 `E_SKELETON_DISCONNECT`；
- 缺少通道峰值时返回 `E_SKELETON_DISCONNECT`。

每条骨架边写出：

```text
两端 peak_id
两端 intensity
intensity_delta
原始距离 distance
粗分箱距离 binned_distance
distance_delta
两端坐标
```

因此边证据可由峰值明细独立复算。精确路径使用细粒度图；近似路径使用 `group_bin_size` 分箱距离，但保留全部真实峰值坐标和关键点，不做静默删除。

## 4. 自适应取舍

每张图观测：

- 全局图像数；
- 每图候选数；
- 每图关键点数、骨架数和热图通道数；
- 画布像素数；
- 精确/近似峰值比较估算；
- 精确/近似驻留内存估算；
- 匹配矩阵驻留单元估算。

裁决优先级显式固定为：

```text
memory_safety > progress > precision
```

硬限制先检查。无法在硬限制内完成时返回 `E_RESOURCE_LIMIT`，绝不通过丢弃图像或关键点继续。

只有同时满足以下条件才走精确路径：

- 图像数不超过 `exact_max_images`；
- 每图候选数不超过 `exact_max_candidates_per_image`；
- 关键点数不超过 `exact_max_keypoints`；
- 精确峰值比较估算不超过 `max_peak_comparisons_per_image`；
- 精确内存估算不超过 `memory_budget_mb`。

否则在近似路径也满足硬限制时降级为近似路径，并写出：

- `degradation_categories`：`PEAK_RESOLUTION`、`MATCHING_STRATEGY`、`GROUPING_RESOLUTION`、`SCALE_DOWNGRADE`、`RESOURCE_DOWNGRADE`；
- `reasons`：超限字段、观测值和上限；
- `adjudication_outcome`：峰值比较节省、内存节省、观测 PCK 增益和指标口径声明。

两条路径使用完全相同的匹配阈值和 PCK/OKS 口径，降级不会改变指标定义。

## 5. 精确参考与路径漂移

参考值是否可计算由独立于所选路径的声明规则决定：

- 每图候选数不超过 `reference_max_candidates_per_image`；
- 精确峰值比较估算不超过 `reference_max_peak_comparisons_per_image`；
- 精确内存估算不超过 `reference_memory_mb`。

满足规则时必须同时运行精确和近似路径，不能声明不可用。不满足时 `cross_path.available=false`，并逐项写出原因。

可比图的跨路径输出：

- 每个通道/关键点的精确与近似坐标、`delta_x`、`delta_y`、欧氏偏差；
- 实例 bbox IoU；
- 每关键点 `exact_pck`、`approx_pck`、`exact - approx` 差谱；
- 全局 PCK 差。

任一坐标偏差超过 `path_tolerance_px` 时返回 `E_PATH_DRIFT`，错误详情包含 `image_id`、通道、关键点索引、两端坐标和偏差量。

## 6. 匹配与指标

精确路径使用 Hungarian 全局最优匹配；近似路径使用按真值实例分块的贪心匹配，并维护全局已用预测集合，保证一对一。候选裁决复用同一策略。

排序和同分规则：

```text
匹配分数降序
-> 候选 source 升序
-> keypoint_index 升序
-> instance_id 升序
```

OKS 相似度：

```text
exp(-d^2 / (2 * sigma_k^2 * (H*W)))
```

`sigma_k` 来自 `keypoint_meta[k].oks_sigma`。PCK 命中：

```text
d <= pck_threshold_px
```

不可见关键点默认排除。每张图输出：

- 总关键点数；
- 可见关键点数；
- 排除的不可见关键点数；
- 实际评估关键点数；
- PCK 命中数和 PCK；
- 已匹配实例数、未匹配真值实例数、平均 OKS、OKS recall；
- 逐实例和逐关键点明细。

PCK 分母严格为“实际评估关键点数”。未匹配真值实例的可见关键点按 miss 计入；预测中的额外实例不改变真值分母。

## 7. 自检

每次管线结束后执行：

- 图像数量和所选路径覆盖；
- 解码通道与分组实例关键点集合一致；
- 每个实例都覆盖全部 K 个关键点；
- 匹配引用的 peak ID 都属于分组峰值集合；
- `visible + excluded == total`（默认排除口径）；
- 指标可从 `keypoint_details` 独立复算；
- 聚合指标可从逐图明细独立复算；
- 精确参考声明与 `cross_path.available` 一致；
- 降级必须带类别。

失败时追加 `E_INCONSISTENT`，并包含 `category` 和 `image_id`。

## 8. 复现实验

`run_experiment.py` 对基线和规模追踪各运行两次，比较：

- 管线报告 `report_sha256`；
- 完整报告的 canonical JSON 字节。

总报告 `content_sha256` 在加入该字段前对全部内容计算。独立进程重复执行会重建同样的样本和报告，可使用文件 SHA-256 和 `cmp` 复核。
