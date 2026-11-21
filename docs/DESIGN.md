# IGEVAL 技术设计

## 1. 目标与不变量

组件解决两类一致性问题：

1. 单点查询与批量评估不得因为缓存、近似或统计量版本不同而得到不同分数。
2. 快照切换、提示词修正和样本追加必须可追踪，旧结果不得覆盖新状态。

核心不变量：

- 批量和单点共用同一精确内核 `igeval.pipeline.run_pipeline`。
- 所有分数先按固定 10 位小数量化，再参与摘要。
- 所有规范 JSON 使用 UTF-8、键排序、无空白、不允许 NaN/Infinity。
- `score_digest` 不包含任何计时或缓存元数据。
- 同分排序键固定为 `(-final_score, sample_id)`。
- 查询只捕获一个不可变快照引用和一个样本修订。
- 旧快照/旧样本修订的计算结果不得写入当前缓存。
- 快照版本和组件版本只允许单调增加。

## 2. 模块边界

| 模块 | 职责 |
| --- | --- |
| `igeval.samples` | 固定种子/画布渲染、样本模型、渲染签名 |
| `igeval.validation` | 输入配置、画布/种子/签名、提示词校验 |
| `igeval.pipeline` | 四级确定性打分、摘要与成本估算 |
| `igeval.snapshot` | 组件校验和、完整快照、版本文件与 `CURRENT` |
| `igeval.cache` | 内容寻址缓存、驻留上限、磁盘校验 |
| `igeval.invalidation` | 失效事件与命中/重算集合 |
| `igeval.engine` | 批量、单点、快照切换、样本变更、资源与并发 |
| `igeval.selfcheck` | 跨模块一致性自检 |
| `igeval.demo` | 进程池完整示例与验收证据 |

## 3. 样本与确定性

### 渲染合同

样本声明：`sample_id`、`seed`、`width`、`height`、`prompt`、`reference`、`truth_grade`、`render_signature`。

`render_image(seed, width, height)` 只使用声明字段：

- `numpy.random.SeedSequence([seed, width, height])`
- 固定顺序的渐变、波、椭圆和矩形绘制
- 固定参数的高斯噪声
- 输出 `uint8`、C 连续、`(height, width, 3)`

`render_signature` 是种子、画布和像素字节的规范 SHA-256。任何字段与图像不一致返回 `E_CANVAS_MISMATCH`。

### 规范摘要

```text
canonical_json(value) =
  json.dumps(value, sort_keys=True, separators=(",", ":"),
             ensure_ascii=False, allow_nan=False)
```

分数统一 `round(value, 10)`，并把 `-0.0` 归一为 `0.0`。因此同一环境中的阶段详情、分数和摘要在序列化后逐字节一致。

## 4. 四级评分

### 4.1 预处理

- 面积平均下采样至配置网格（默认 `16x16`）。
- 计算 RGB 均值/标准差、亮度、饱和度、8 桶亮度直方图。
- 划分为 `4x4` 块，每块形成 12 维特征：

```text
[mean_r, mean_g, mean_b,
 std_r, std_g, std_b,
 mean_luma, mean_edge, mean_saturation,
 x_position, y_position, mean_luma * mean_saturation]
```

特征做 L2 归一化并量化。阶段分为对比度相似度、饱和度相似度、色彩平衡的加权结果。

### 4.2 语义对齐

- 文本 NFKC、转小写，使用 `[a-z0-9]+|[\u3400-\u4dbf\u4e00-\u9fff]` 分词。
- 每个词项 `SHA-256("igeval.token-embedding/1\0" + token)` 产生 12 个有符号分量，再 L2 归一化。
- 每个词项与所有图像块计算余弦相似度，取最佳块。
- 词项权重 = 快照源权重 × `term_decay ** position`。
- 候选数为 `块数 × 词项数`，用于成本估算。

### 4.3 结构一致性

- 亮度图梯度得到边缘密度和横纵方向能量比。
- 4x4 区域亮度归一化为区域分布。
- 目标区域分布、目标边缘密度、目标方向比由文本摘要和结构参数版本确定性导出。
- 区域相似度使用 L1 距离；边缘和方向使用指数衰减差。
- 按快照中的边缘/区域/方向权重复合。

### 4.4 归一化与聚合

```text
raw = Σ fusion_weight[i] * stage_score[i]
z = (raw - normalization.mean) / normalization.std
final = sigmoid(z), clipped to [0, 1]
predicted_grade = 1 + min(4, floor(final * 5))
```

归一化统计量、融合权重、对齐权重和结构参数均来自查询开始时捕获的快照。

## 5. 摘要关系

```text
stage_digest:
  preprocess -> image hash + config fingerprint + features
  semantic   -> preprocess digest + text digest + alignment version/checksum + matches
  structure  -> preprocess digest + text digest + structure version/checksum + metrics
  quality    -> three stage digests + normalization version/checksum + aggregation

feature_digest = H(preprocess digest, semantic digest, structure digest)

cache_key = H(
  sample_id,
  image_content_hash,
  text_term_digest,
  alignment version + checksum,
  normalization version + checksum,
  structure version + checksum,
  snapshot_id,
  precision,
  scoring config fingerprint
)

score_digest = H(
  sample_id,
  snapshot_id,
  precision,
  feature_digest,
  cache_key,
  four stages,
  final_score,
  predicted_grade
)
```

`score_digest` 是批量与单点一致性比较的主键；`cache_key` 是缓存身份；`feature_digest` 连接特征与缓存条目。

## 6. 快照协议

快照对象结构：

```json
{
  "schema": "igeval.snapshot/1",
  "snapshot_version": 1,
  "snapshot_id": "<overall sha256>",
  "components": {
    "normalization": {"version": 1, "checksum": "<sha256>", "...": "..."},
    "alignment": {"version": 1, "checksum": "<sha256>", "...": "..."},
    "structure": {"version": 1, "checksum": "<sha256>", "...": "..."}
  },
  "checksum": "<overall sha256>"
}
```

组件校验和计算时排除自身 `checksum`；整体校验和覆盖三个已校验组件和整体版本。持久化路径：

```text
<runtime>/snapshots/snapshot-v0000000000000001-<overall>.json
<runtime>/snapshots/CURRENT
```

`CURRENT` 使用同目录临时文件、`fsync` 和 `os.replace` 原子替换。旧版本文件保留用于审计。切换在状态锁内完成校验、持久化和内存引用替换。

## 7. 缓存与失效

缓存文件：

```text
<runtime>/cache/entries/<key[:2]>/<key>.json
```

条目包含完整结果、特征摘要、分数摘要、快照版本和内容校验和。载入时重新校验内容校验和与缓存键。内存缓存受条目数和序列化字节数限制；磁盘条目可读但若超过驻留上限则不增加驻留内存。

失效事件示例：

```json
{
  "event_id": 1,
  "kind": "snapshot_switch",
  "affected_sample_ids": ["sample-001", "..."],
  "invalidated_cache_keys": ["..."],
  "cache_hit_sample_ids": [],
  "recomputed_sample_ids": ["sample-001", "..."],
  "pending_sample_ids": [],
  "coverage_ok": true
}
```

事件按固定顺序编号，不使用系统时间。`mark_recomputed` 只对实际重算生效；`mark_hit` 只对尚未重算的受影响样本生效，因此两个集合始终不相交。

## 8. 并发状态模型

状态：

- `_snapshot`：不可变完整快照
- `_samples`：样本映射
- `_sample_revisions`：每样本单调修订号
- `_sample_cache_keys`：样本到缓存键
- `_events`：失效事件

锁顺序固定为 `state lock -> cache lock`，事件锁独立且不反向获取状态锁。查询捕获 `(sample, revision, snapshot)` 后完全使用捕获值计算。缓存写入前再次检查当前快照 id 和样本修订，避免切换/修正期间的旧结果写回。

批量使用 `spawn` 上下文。每个 worker 接收完整配置、完整快照和无图像样本线上表示，重新渲染并校验签名后运行同一内核。父进程按分数和样本 id 排序，不依赖 worker 完成顺序。

## 9. 错误归因

- 校验错误阶段为 `validation`，包含 `sample_id` 和字段。
- 阶段异常包装为 `StageFailure(stage, cause)`。
- 已知阶段异常进入 `errors_by_stage[stage]`。
- 无阶段异常进入 `unattributed_errors`，不会被丢弃。
- 批量保留已完成样本，`status` 可为 `ok`、`partial`、`error`。
- 单点预算/资源错误仍返回四级结果和 `completed_stages`。

## 10. 自检顺序

1. 批量若为空且状态错误，记录 `batch_status`。
2. 比较四级 `stage_sample_ids` 与成功样本集合。
3. 检查四级和最终分数范围。
4. 检查排序键与 rank。
5. 对每个结果执行热缓存查询和强制冷查询。
6. 按级顺序比较批量、冷查询、热查询；首个偏差即失败项。
7. 检查 `feature_digest`、`cache_key`、`score_digest`。
8. 检查缓存条目中的键、摘要、图像哈希、文本摘要和快照 id。
9. 检查每个失效事件命中/重算集合互斥且覆盖全部受影响样本。

任一项失败返回 `E_INCONSISTENT`，并给出 `category`、`sample_id`、`stage` 和完整 `failures` 列表。

