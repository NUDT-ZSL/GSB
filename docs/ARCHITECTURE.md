# 实现结构

## 数据流

```text
gen_samples.py
  -> samples/edit_tasks.jsonl + samples/params.json
  -> geometry / imaging / tasks
  -> operators（同源边界计划、补全、着色、融合）
  -> pipeline（整幅路径、分块路径、精度诊断）
  -> scheduler（资源估算、预算裁决、ProcessPoolExecutor）
  -> cache + checkpoint + workbench（内容哈希、修订失效、恢复）
  -> selfcheck / reports / cli（重放、自检、JSON 报告）
```

## 五阶段 DAG

1. `S1_mask_boundary`：校验多边形，栅格化封闭区域，输出边界像素、8 邻域上下文、逆距离权重、leave-one-out 边界残差和颜色归一化统计量。
2. `S2_completion`：最近边界源颜色按固定步长向加权边界目标推进；逐像素保存源索引、权重和颜色贡献。同时执行整幅与分块补全并比较。
3. `S3_style`：使用同一份上下文均值/尺度和 3×3 颜色迁移矩阵做归一化迁移，再按 `style_strength` 混合；保存 3×3 颜色映射贡献。
4. `S4_fusion_blocks`：使用全局距离羽化权重融合原图和风格化结果；执行分块着色/融合、拼接、SHA-256 字节比较并生成首差坐标。
5. `S5_metrics`：聚合编辑量、接缝代价、分块偏差谱、三档精度误差传播和可重算明细。

## 分块/整幅一致性

`build_region_plan()` 只构造一次全局计划。分块路径通过 `subset_plan()` 选择像素行，但保留同一个 `mean`、`scale`、矩阵、残差数组和归一化系数。K 个边界源的累加在两个路径中都按固定顺序执行，因此 float64 下不依赖块大小或块调度。

若任何 stage 不同，`compare_full_block()` 返回：

- `code = E_BLOCK_MISMATCH`
- 首个差异的全局 `coordinate_xy` / `coordinate_yx_or_yxc`
- `actual_value` 与 `reference_value`
- 两路径输出数组的 SHA-256

## 精度档

- `float64`：参考真值。
- `float32`：每个算子后保持 float32 存储和运算。
- `fixed`：按任务显式 `quant_scale` 与 `rounding_mode` 生成 int64 量化码并反量化。

每级输出 MAE、最大绝对误差、RMSE、相对 RMSE、相对前一级放大比。S5 缓存保存 `<profile>__actual_<level>` 和 `<profile>__ref_<level>` 数组，`recompute_stage_diagnostics()` 可仅从这些 NPZ 明细重算全部指标。

## 调度与预算

`estimate_task()` 估算：

- `pixel_buffer_bytes`：`H × W × 3 × 8 × buffer_count`
- `boundary_context_bytes`：边界周长对应的上下文样本缓冲
- `per_block_intermediate_bytes` 与 `block_intermediate_bytes`
- `resident_bytes`

`adjudicate_batch()` 按 `edit_id` 稳定排序，记录 dispatch/merge 顺序、worker 收缩、`SERIAL`、`SMALLER_BLOCK` 和拒绝码。`run_process_pool()` 使用 fork context 的 `ProcessPoolExecutor`，通过 `executor.map()` 保持提交和归并顺序。

## 缓存、修订和检查点

缓存对象以产物内容哈希命名：

- `runtime/<run>/cache/objects/<artifact_hash>.json`
- `runtime/<run>/cache/objects/<artifact_hash>.npz`

缓存键包含图像哈希、掩码摘要、`style_strength`、`iterations`、`block_size`、`dtype_profile`、`revision`、上游产物哈希和颜色矩阵哈希。

style-only 修订复用 S1/S2，只重算 S3–S5 及块下游；mask 修订使该任务 S1–S5 全部失效；未变化任务直接命中五阶段缓存。旧键查询由 `detect_stale_result()` 返回 `E_STALE_RESULT`、`edit_id` 和 `stale_fields`。

检查点位于 `runtime/<run>/checkpoints/checkpoint_XXXXXXXX.json`，版本单调递增，保存已完成 edit_id 集合、阶段产物哈希、父检查点哈希和 checksum。
