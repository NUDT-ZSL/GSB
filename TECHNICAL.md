# 技术说明

## 1. 场景数据与确定性

场景清单是 JSONL，每行至少包含：

```json
{
  "scene_id": "scene-alpha",
  "scene_revision": 1,
  "camera_poses": [
    {
      "camera_id": "scene-alpha-cam-0",
      "rotation": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
      "translation": [2.8, 0.1, 1.65],
      "intrinsics": [[24, 0, 11.5], [0, 24, 8.5], [0, 0, 1]],
      "observation_seed": 1001,
      "image_height": 18,
      "image_width": 24
    }
  ],
  "point_cloud_seed": 1103,
  "texture_seed": 2203,
  "geometry_mode": "terrain",
  "target_face_budget": 72,
  "priority": 30
}
```

`rotation`/`translation` 是 camera-to-world 位姿。相机坐标的 `x` 向右、`y` 向下、`z` 向前。

随机数不使用全局 NumPy RNG，也不使用 Python 随机化哈希。`derived_seed(*parts)` 对字符串部件做 SHA-256，取前 128 bit 作为 `PCG64` 种子。相机遍历、点索引、顶点索引、边候选都先显式排序。

规范化序列化规则：

- `numpy.ndarray` 转成 tagged object，保存 little-endian dtype、shape 和 base64 C-order 字节；
- 所有字典键排序，JSON 使用紧凑分隔符，禁止 NaN/Infinity；
- 顶层浮点通过 Python 的最短可回读表示写入；
- 内容校验和是规范化字节的 SHA-256。

因此同一 Python/NumPy 组合下，同一场景清单与配置的阶段产物字节稳定。

## 2. 点云配准

### 数据生成

`generate_world_geometry` 生成模式无关的规则网格点云与三角拓扑。`point_cloud_seed` 同时控制曲面相位和小幅抖动。每台相机从世界点云中按 `observation_seed` 采样约 72% 的点，变换到相机坐标并加入 `registration_noise` 高斯噪声。

相机图像是固定种子驱动的 `float64 [H,W,3]` NumPy 数组，不读取真实图片。

### ICP

初值是相机清单中的 world-from-camera 矩阵。每轮：

1. 变换相机点云到世界坐标；
2. 对每个源点找欧氏距离最近的目标点；距离同分时 `argmin` 保留较小目标索引；
3. 用 Kabsch/SVD 求增量旋转和平移，反射时翻转最小奇异向量；
4. 组合变换，更新量小于 `icp_tolerance` 时停止。

残差定义为：

```text
RMSE = sqrt(mean_i ||T * source_i - nearest_target(T * source_i)||^2)
```

`registration_residual()` 只接收世界点云、相机坐标源点和 4×4 变换矩阵，可独立复算。阶段 1 与阶段 5 都做该复算。

## 3. 融合与简化

### 融合

每台相机的注册点云按 `(camera_id, point_index)` 串行累加到最近的基顶点：

```text
w_ij = exp(-||p_ij - v_j||^2 / (2 sigma^2))
normalized_w_ij = w_ij / sum_i w_ij
v'_j = (1 - alpha) v_j + alpha * sum_i normalized_w_ij p_ij
```

累加顺序固定，不使用可能随调度变化的并行归约。`sum_i normalized_w_ij` 对 1 的偏差形成“融合权重归一化”误差级。

`geometry_mode` 在融合后应用：

- `terrain`：保留融合网格；
- `ellipsoid`：按网格 UV 映射到固定种子控制半径的椭球，并保留融合相对基网格的微小修正。

因此仅切换 `geometry_mode` 不需要重算配准，但会改变阶段 2–5。

### 简化

采用确定性边坍缩：

1. 从面片提取无向边，按顶点索引排序去重；
2. 代价为边长平方；
3. 候选排序键为 `(cost, min_vertex, max_vertex)`；
4. 选择首个能减少面片数的有效坍缩；
5. 顶点和 UV 取中点，移除退化面与重复面；
6. 面数进入预算后停止并压缩未引用顶点。

若仍超预算，抛 `E_FACE_BUDGET_EXCEEDED`，详情含 `scene_id`、`over_budget_faces`、`faces_after`、`target_face_budget`。

## 4. 纹理烘焙与分块一致性

纹理路径先在 UV 域三角光栅化，得到每个纹素对应的表面点和法向；再对每台相机：

1. 世界点投到相机坐标和像素坐标；
2. 双线性采样相机观测图；
3. 权重为 `max(0.02, |normal·view|) / (1 + 0.15 z)`。

所有相机贡献被组织成：

```text
contributions[H,W,C,3]
weights[H,W,C]
base_colors[H,W,3]
```

整段路径和分块路径共用同一个 `_integrate_pixel`：

```text
sum_w = sum_c weights[c]
color = sum_c weights[c] * contributions[c] / sum_w
```

相机顺序固定，每个像素独立归约，因此块边界不参与归一化分母。分块只是改变像素遍历分组，不改变任何单个像素的运算顺序。两路结果用 `bytes` 全等比较，而不是容差比较。

一旦不一致，`compare_texture_paths` 抛 `E_TEXTURE_BLOCK_MISMATCH`，输出：

- 首个偏差像素 `[row, col]`；
- 该像素 RGB 绝对偏差；
- 每像素最大通道绝对偏差的 min/mean/p50/p90/p99/max；
- 16 桶直方图。

多视角检查对每个可见相机数 ≥ 2 的纹素计算加权颜色方差，报告 mean/p95/max spread 与是否落在配置容差内。

## 5. 误差传播谱

整段 float64 纹理是参考真值。误差谱固定四级：

| 级别 | 绝对偏差 | 参考尺度 | 区间 |
|---|---|---|---|
| `registration` | 配准最近邻 RMSE | 点云 RMS 半径 | 所有点最近邻距离的 min/max |
| `fusion_weights` | 每顶点归一化权重和与 1 的最大偏差 | 1 | 各顶点偏差 min/max |
| `simplification` | 融合顶点到简化网格最近顶点的 RMS 位移 | 变形网格包围盒对角线 | 各顶点位移 min/max |
| `texture_block` | 分块/整段每像素最大通道绝对差的最大值 | 整段纹理最大绝对值 | 每像素最大通道差 min/max |

相对偏差为绝对偏差除以参考尺度。放大比为当前级相对偏差除以前一级相对偏差；第一级为 1，前级为 0 时用中性值 1，当前级为 0 时为 0。主导来源取相对偏差最大者，同分按级别顺序。

构造 `ErrorSpectrum` 时会检查：

- 级别顺序必须为上述四级；
- 绝对偏差、相对偏差、放大比、尺度非负且有限；
- 每个 `error_interval` 必须满足 `lower <= upper`。

违反时抛 `E_ERROR_SPECTRUM_INVALID`，字段形如 `levels[2].simplification.error_interval`。

## 6. 门限稳定性

`evaluate_threshold` 支持：

- `strict`：原始 IEEE 比较，`le` 为 `metric <= threshold`，`ge` 反向；
- `tolerance`：容差带

```text
margin = abs_tolerance + rel_tolerance * max(|threshold|, tiny)
le: metric <= threshold + margin
ge: metric >= threshold - margin
```

若 `0 < |value| < np.finfo(float64).tiny`，保护开启时把 value 规范为 `0.0`。非法策略抛 `E_THRESHOLD_POLICY_INVALID`，负数/NaN/Inf 容差抛 `E_TOLERANCE_INVALID`，两者均带字段名。

流水线对配准残差使用 `le` 门限，对简化度使用 `ge` 门限；判定结果写入阶段 1/2 产物和最终快照摘要。

## 7. 增量传播、缓存与快照

### 缓存键

每个阶段键由以下材料规范化哈希：

```text
stage
scene_content_hash
camera_pose_digest
geometry_mode
target_face_budget
upstream_artifact_hash
config_digest
effective_face_budget
```

上游根为字符串 `ROOT`；后续阶段的 upstream 哈希为上一阶段 payload 校验和。

增量的“未受影响已提交阶段”从快照引用直接复用，即使新的缓存键材料因预算/模式字段变化而不同。复用时会把这些已提交字节物化到当前内容键下，不重新执行算法；从零全量重放会证明该阶段 payload 字节未变。这是“已提交产物复用”和“缓存键寻址”两个层次的区分。

### 快照

快照 JSON 是：

```json
{
  "format": "sceneopt-snapshot-v1",
  "checksum": "<canonical payload sha256>",
  "payload": {
    "format": "sceneopt-snapshot-v1",
    "version": 2,
    "config_digest": "...",
    "scenes": {"...": {"stages": {"...": {"cache_key": "...", "checksum": "..."}}}},
    "summary": {"...": "..."}
  }
}
```

提交时：

- 先验证完整校验和；
- 版本必须严格大于当前版本；
- 同版本同校验和为幂等 no-op；
- 同版本不同校验和或更低版本抛 `E_SNAPSHOT_ROLLBACK`；
- 读取/校验失败抛 `E_SNAPSHOT_CORRUPT`；
- 新快照整文档写入 `snapshot-vNNNNNNNN.json`，再原子替换 `snapshot.json`；
- 整个运行与提交由 `O_CREAT|O_EXCL` 锁保护，锁冲突立即可见 `E_STAGE_BUSY`。

### 自检与全量重放

运行级自检检查：

- 每个阶段的 scene_id 集合与清单一致；
- 每阶段“重算集合”和“复用集合”不交；
- 两者并集覆盖全部受影响场景；
- 全局重算/复用集合也满足不交与覆盖。

增量运行默认在临时空缓存中从零计算同一修订清单，逐场景逐阶段比较 payload 校验和。首个不一致抛 `E_INCREMENTAL_INCONSISTENT`，详情含 `first_deviating_scene_id`。

## 8. 资源上限

配置 `ResourceLimits`：

- `max_faces_per_scene`：预算超限时可降级到上限并标记，或直接 `E_RESOURCE_LIMIT`；
- `max_resident_bytes`：按简化网格顶点/面/UV 与纹理、权重数组估算驻留字节，超限 `E_RESOURCE_LIMIT`；
- `max_parallel_recompute`：限制并行重算场景数；超出时降级到上限并写入 `resource.degradations`，禁止降级时 `E_RESOURCE_LIMIT`。

当前默认执行器为确定性串行场景循环，请求并行度只影响调度上限与降级标记，不改变产物。

