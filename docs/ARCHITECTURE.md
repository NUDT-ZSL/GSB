# 确定性 OCR 管线：架构与裁决说明

## 1. 数据流与三阶段

```
固定种子 RNG
   │  sample.generate_page
   ▼
灰度页面 + 真值框/真值串 + 任务清单(JSONL)
   │  detection.detect_lines（行投影 + 4 邻域连通域 + 确定性合并/吸附）
   ▼
候选行框（整数像素，y 升序再 x 升序）
   │  recognition.collect_norm_stats（页面级二值空间全局统计）
   │  recognition.recognize_line（字形槽切分 + 模板匹配 + 量化）
   ▼
逐行识别串 / 置信度 / 候选明细
   │  recognition.deduplicate_lines（IoU 去重）+ metrics.compute_metrics
   ▼
页面级结果与指标 → SnapshotStore 版本化整体提交
```

所有随机来自 `np.random.default_rng(SeedSequence(...))`，由整数种子与盐值派生；
业务逻辑不读取系统时间，`time.perf_counter` 仅用于观测延迟，不参与任何
识别、排序、哈希或缓存决策。持久化统一使用 `sort_keys` 的规范 JSON。

## 2. 关键一致性设计

- **归一化口径统一**：`collect_norm_stats` 在识别前对整页所有行像素一次性
  汇总二值空间的均值/标准差与墨像素数，并生成 `digest`。整页识别与任意分块
  识别都接收同一个 `NormStats`，块边界不存在独立统计量。
- **去重/量化口径统一**：`deduplicate_lines`、置信度 `round(..., 6)` 与候选
  排序（分数降序、字符升序）是纯函数，分块拼接与整页调用同一函数。
- **逐字节比对**：`chunking.assert_byte_identical` 比较每行的规范 JSON 字节，
  首个差异即抛 `E_CHUNK_MISMATCH`，并给出 page_id、行号与差异字段类别，
  不做任何容差放宽。

## 3. 缓存键与失效

缓存键（`cache.build_cache_key`）纳入：行号、页面图像内容哈希、行框摘要、
模板库版本号与校验和、识别参数指纹、分块大小。任一维度变化都会改变键。

失效是“按受影响集合”精确删除，而非全清：

- 页面像素变化 → 按 `image_hash` 命中；
- 行框变化 → 按 `boxes_summary` 命中；
- 模板库升级 → 按库版本号/校验和命中；
- 字符集收缩 → 按库版本关联条目命中。

校验器强制：重算集合与复用集合无交集，且并集等于全部受影响行。

## 4. 精度/延迟/内存裁决

`chunking.plan_chunk_size` 的显式优先级为：

1. `recognition_correctness`：任何候选分块都产出逐字节相同结果，正确性不可交易；
2. `memory_ceiling`：优先保留低于驻留内存上限的分块；
3. `first_line_latency`：在安全集合中选首行延迟估计最小者（即更小分块）；
4. `reduction_overhead`：平局时再比归约开销。

若所有分块都超内存上限，不允许牺牲结果，选择内存占用最小者并在计划中记录
违约依据。计划含裁决理由、所选分块、受影响行范围与可复现哈希。

## 5. 快照版本化

三类快照 `template_library` / `recognition_params` / `page_results` 均带单调
递增版本号与内容 SHA-256 校验和。提交走临时文件 + `os.replace` 原子整体替换；
版本回退、校验和不符、JSON 截断均抛 `E_SNAPSHOT_CORRUPT` 且拒绝部分数据。
进程内非阻塞锁保证同一时刻仅一个提交生效，后到者立即得到 `E_STAGE_BUSY`。

## 6. 延迟预算

`engine.OCREngine.query_line` 在真实本地识别循环外包裹 `perf_counter` 计时，
返回像素数、匹配候选数、估算成本、已完成阶段与已就绪行号。超预算抛
`E_LATENCY_BUDGET`；演示用 0ms 预算使任何正耗时必然超预算，无需线程或 sleep。

## 7. 错误码

| 代码 | 触发场景 |
| --- | --- |
| `E_CHUNK_MISMATCH` | 分块与整页结果首个逐字节差异 |
| `E_LATENCY_BUDGET` | 单行查询真实计时超预算 |
| `E_SNAPSHOT_CORRUPT` | 快照版本/校验和/解析损坏 |
| `E_STAGE_BUSY` | 快照提交并发冲突，立即拒绝 |
| `E_INCONSISTENT` | 跨模块一致性自检失败 |
| `E_INVALID_INPUT` | 任务清单/查询参数非法 |
| `E_CONFIG` | 配置或字符集版本非法 |
