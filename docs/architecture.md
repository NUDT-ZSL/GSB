# 架构与一致性设计

## 1. 模块边界

| 模块 | 职责 |
| --- | --- |
| `samples.py` | 固定种子生成 RGB 画面、骨架、可见/遮挡标记、热图真值与观测张量，写 JSONL 清单 |
| `heatmap.py` | 热图通道校验、局部峰值检测、平台同分规则、亚像素加权质心、坐标量化 |
| `grouping.py` | 骨架树校验、根通道锚点、独占关键点、邻近距离与实例直径约束 |
| `scoring.py` | 同一归一化口径的实例评分和场景指标聚合 |
| `pipeline.py` | 全场景与区域裁剪的统一阶段编排、阶段缓存键、首块预算 |
| `consistency.py` | 峰值/实例规范化字节裁决与首个差异明细 |
| `snapshots.py` | 三类快照、校验和、单调版本、原子替换、历史只读版本 |
| `invalidation.py` | 三类变更的受影响阶段、区域与实例集合 |
| `service.py` | 场景生命周期、预览编排、降级、过期拒绝与重放 |
| `gui.py` | 画布、区域网格、异步首块/其余区域、评分表、版本与业务码 |

## 2. 确定性峰值与精修

检测使用 `peak_neighborhood_radius=1` 的 8 邻域局部极大值。对于平台区域，只接受 `(row, column)` 顺序最前的像素：

1. 候选必须不小于全部邻域；
2. 对行/列顺序更早的邻域，候选必须严格大于。

峰值最终排序键为：

```text
(-intensity, channel, y, x)
```

因此强度相同时先按通道升序，再按像素行优先、列优先。

亚像素精修在每个峰值周围显式窗口 `(2 * refine_radius + 1)^2` 内做非负加权质心。窗口先在全局热图坐标中截断；截断时记录 `window_clipped=true`。结果用半向上规则量化到 `quantization_decimals` 位：

```text
floor(value * 10^p + 0.5) / 10^p
```

## 3. 区域预览的同口径证明

全场景和区域路径都调用 `detect_and_refine()`、`group_instances()`、`score_instances()`，不存在第二套边界策略或评分折中。

对核心区域 `R`，裁剪框扩展：

```text
halo = ceil(max_instance_diameter + refine_radius + peak_neighborhood_radius + 2)
crop = intersect(R expanded by halo, canvas)
```

元数据声明 `max_instance_diameter`，分组时对每个新候选检查其与实例内所有已选峰值的距离。因此任何与 `R` 相交的完整实例、以及可能影响该实例连接的候选，都落在安全光环内。区域检测额外丢弃紧贴裁剪边界的峰值，避免人工边界产生峰值；核心区域峰值与全场景一致。

区域输出仅保留任一关键点中心落在 `R` 内的完整实例。全场景结果使用完全相同的“任一点落在 R 内”过滤规则，因此两边比较对象相同。跨区域拼接按 `instance_id` 去重；同一实例被多个区域返回时必须字节相同。

## 4. 逐字节裁决

实例业务载荷固定为：

```json
{
  "instance_id": "...",
  "keypoints": ["channel/y/x/intensity/refined_y/refined_x/window_clipped"],
  "score": 0.123456789,
  "matched": true
}
```

使用排序键、紧凑分隔符、UTF-8 和稳定浮点格式序列化。比较直接比较字节；不设置 epsilon。首个差异记录：

- `instance_id`
- `difference_type`：`keypoints`、`score`、`matched`、`batch_only`、`preview_only`
- 批处理侧与预览侧完整实例明细

峰值差异先于实例差异裁决，返回 `E_PEAK_MISMATCH` 和首个偏差峰值的通道、原始坐标及两侧精修明细。

## 5. 快照与原子切换

三类快照：

| 类别 | 内容 | 下游影响 |
| --- | --- | --- |
| `heatmap_observation` | 压缩 float32 热图张量 | 峰值及全部下游 |
| `keypoint_metadata` | 通道、骨架树、连接阈值、人员关键点元数据 | 分组、评分、指标 |
| `scoring` | 匹配阈值、权重、归一化、评分精度 | 评分、指标 |

快照文档包含 `scene_id/category/version/checksum/payload`。校验和覆盖场景、类别、版本与完整载荷。提交总是：

1. 校验版本严格递增；
2. 校验载荷结构；
3. 写入同目录临时文件并 `fsync`；
4. `os.replace` 替换当前快照；
5. 读回验证版本与校验和；
6. 同时保留不可变历史版本，供结果重放读取，但历史版本不能提交为当前版本。

## 6. 阶段缓存与定向失效

缓存键：

```text
peaks = H(scene, region/full, heatmap_checksum, quant, refine_window, threshold)
group = H(scene, peaks_key, metadata_checksum)
score = H(scene, group_key, scoring_checksum)
```

失效矩阵：

| 变更 | 复用 | 失效 | 受影响范围 |
| --- | --- | --- | --- |
| 仅评分参数 | 重建、峰值、精修、分组 | 评分、指标 | 全部实例评分 |
| 关键点元数据 | 重建、峰值、精修 | 分组、评分、指标 | 结构变化为全区域；人员局部修正按旧/新关键点包围盒映射区域和实例 |
| 热图观测 | 无 | 峰值、分组、评分、指标 | 全区域、全实例 |

实例记录写入 `cache_key`、`snapshot_versions`、`snapshot_checksums`、`node_path`。读取旧记录时，当前版本或校验和任一变化即返回 `E_STALE_RESULT`。重放使用记录中的分组缓存节点和对应历史评分/元数据快照重新计算，要求实例集合与评分再次逐字节一致。

## 7. 延迟预算

首块观测为真实 `perf_counter_ns()` 区间。估算记录包含：

- 核心区域像素数；
- 扩展后实际参与计算像素数；
- 峰值候选数；
- 精修窗口单元格数；
- 每类工作量的显式单位成本与总估算毫秒。

预算判定只使用观测毫秒。超限抛出 `E_LATENCY_BUDGET`，并附带已完成区域列表，不使用睡眠或人为延迟。

## 8. 资源与并行请求

- 单场景像素总量超限：`E_RESOURCE_LIMIT`；
- 峰值驻留内存估算超限：`E_RESOURCE_LIMIT`；
- 并行预览请求数超过上限：内部降级为串行，标记 `DEGRADED_SERIAL_PREVIEW`，不伪装成并行结果。

## 9. 图形工作台

PySide6 工作台展示：

- 确定性 RGB 场景与骨架；
- 5×5 区域网格和已就绪状态；
- 中心首块观测/估算/相对批处理加速比；
- 当前实例的逐项评分与匹配标记；
- 三类快照版本；
- 最近业务码；
- 评分、元数据、热图三类版本升级按钮及其受影响区域数量。

首块和其余区域在 `QThread` 中执行，避免阻塞界面。

