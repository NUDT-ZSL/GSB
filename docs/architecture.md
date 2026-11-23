# MatForge 技术说明

## 1. 数据与确定性边界

`MaterialParams` 固定材质标识、种子、基础色、粗糙度、法线强度、接缝参数和 `NoiseParams`。同一组参数与同一 `BlockPlan` 下，整幅和切片路径都只使用显式输入：

- 噪声晶格哈希只依赖整数坐标、种子、倍频编号和显式相位；
- 倍频权重为归一化的 `persistence ** octave`；
- 不使用系统时间、进程级随机数或全局可变随机状态；
- 数组摘要绑定 dtype、shape 和 C 序原始字节；
- 所有磁盘写入先写临时路径，再用 `os.replace` 原子替换。

内置 `mineral-slate`、`oxidized-iron`、`desert-sandstone` 三个固定种子预设；`presets` 命令列出摘要，`render --preset` 可直接输出独立材质样本。它们共享同一条流水线和确定性约束。

## 2. 五阶段流水线

1. **参数校验与全局归一化**：验证范围与有限性；从按种子散列出的 4096 个全局采样点计算均值/标准差，得到唯一的 `NormalizationStats`。这一统计不依赖块网格，是整幅和全部切片的同一归一化来源。
2. **多倍频噪声场**：像素坐标先转换为全局 `(x + 0.5) / width`、`(y + 0.5) / height`，再乘以倍频频率并叠加参数相位与由种子派生的每倍频相位。晶格值使用 SplitMix64 风格的 64 位整数散列和五次平滑插值。
3. **材质通道映射**：归一化高度结合基础色、粗糙度对比和 Lambert 光照生成 RGB；法线来自高度的中心差分。同一映射函数服务整幅和切片。
4. **分块切片**：每块按全局像素区间独立计算。噪声在全局坐标求值；法线计算向四周扩展 1 像素 halo 后裁回块区间，避免块边缘退化为单边差分。
5. **接缝归一化与拼接**：接缝权重由像素到全局内部网格线的距离、`seam_width` 和 `seam_strength` 计算。切片不持有“块内相对边界”，因此拼接结果与整幅共享同一权重来源。

输出为 `H x W x 7` 的 `uint8` 通道：`[R,G,B,roughness,nx,ny,nz]`。

### 整幅/切片等价不变量

```text
noise(tile, x, y)        == noise(full, x, y)
normalize(tile, x, y)    == normalize(full, x, y)
seam_weight(tile, x, y)  == seam_weight(full, x, y)
channels(tile, x, y)     == channels(full, x, y)
```

测试通过 `stitched.tobytes() == full.channels.tobytes()` 裁决。

## 3. 首块观测与成本记录

`preview(block_id, budget_ms)` 的实际耗时由围绕缓存查询、噪声/通道生成和缓存落盘的 `time.perf_counter()` 观测。记录同时包含：

- 块内像素数、含 halo 像素数；
- 倍频数；
- 接缝邻域像素数；
- `cost-v1` 估算系数和估算毫秒；
- 计算耗时与总查询耗时、来源（生成/缓存/状态复用）；
- 预算判定。

当实际耗时大于预算，切片仍会被提交到已完成清单，然后抛出 `E_LATENCY_BUDGET`，`details.completed_blocks` 给出完整清单。已就绪后的再次查询单独计时，通常为亚毫秒级。

## 4. 内容寻址缓存

最终切片缓存键：

```text
SHA256({
  schema,
  params_digest,
  seed,
  plan_digest,
  block_grid(width,height,tile_width,tile_height,rows,cols,block),
  octave_config_digest,
  normalization_digest,
  upstream_hash
})
```

`upstream_hash` 来自实际噪声块字节、归一化摘要和倍频配置。另有一个不读取上游字节的语义键，用于在生成前预测缓存命中；它包含相同的栅格参数、种子、计划、块区间和上游描述摘要。

缓存对象路径为 `objects/<content_hash[:2]>/<content_hash>/`，保存：

- `channels.npy`：材质通道；
- `seam.npy`：接缝归一化权重；
- `meta.json`：缓存键、参数修订、计划版本、节点路径、上游摘要、通道/接缝摘要。

读取对象时重新计算文件 payload 哈希、内容哈希、通道摘要和接缝摘要，任一不符返回 `E_CACHE_CORRUPT`。

## 5. 修订失效与后缀

每个块的语义键包含实际影响像素的参数（不含 `revision`、`name`、`metadata`）。修订到达时：

1. 为新参数与新计划重新计算所有块的语义键；
2. 比较旧就绪记录，得到失效块；
3. 取最早失效块，形成 `[first_affected, block_count)` 的重算后缀；
4. 只复用严格位于该后缀之前的块，后缀中的块即使个别键未变也按顺序重算；
5. 输出 `affected`、`affected_suffix`、`recomputed`、`reused` 与覆盖关系。

因此：

- 纯展示字段修订：`affected=[]`，全部块复用；
- 全局基础色/噪声/法线等修订：全部块属于受影响后缀，全部重算；
- 任何修订的复用集合与重算集合均无交集，并集覆盖全部块。

这条保守规则保证不会把依赖旧全局统计或旧语义键的块混入新整幅结果。

## 6. 快照

快照文档包含三个独立组件：

```json
{
  "params": {"version": 3, "checksum": "...", "value": {}},
  "plan":   {"version": 2, "checksum": "...", "value": {}},
  "ready":  {"version": 42, "checksum": "...", "items": []},
  "checksum": "...",
  "schema": "matforge.snapshot.v1"
}
```

- 每个组件版本必须为正整数且单调不降；
- 每个组件的规范 JSON 摘要独立校验；
- 总摘要绑定三个组件；
- 提交是整文件临时写入后的原子替换；
- 同版本内容变化视为 `E_SNAPSHOT_CORRUPT`；
- 任一组件版本下降视为 `E_SNAPSHOT_ROLLBACK`；
- 启动时从有效快照整体重建参数、计划和就绪清单，不增量修补。

## 7. 过期切片与重放

`ReadySlice` 和快照条目记录：

- 块编号和全局像素区间；
- 最终缓存键与语义键；
- 内容哈希、通道摘要、接缝摘要、上游摘要；
- 参数修订、参数摘要、计划版本与摘要；
- 节点路径和估算记录。

若记录的参数修订/摘要或计划摘要与当前状态不同，`assert_slice_fresh`、`inspect_stale` 和缓存拼接路径会返回 `E_STALE_SLICE`，并给出材质、块号、过期版本、当前版本、缓存键和节点路径。

`replay_slice` 不只是读回文件，而是用当前同口径流水线重新生成目标块，再核对：

```text
regenerated.channel_digest == recorded.channel_digest
regenerated.seam_digest    == recorded.seam_digest
regenerated.cache_key      == recorded.cache_key
```

## 8. 并发与资源

- `preview_many` / `request_remaining` 使用 `ThreadPoolExecutor`；
- 一个 `BoundedSemaphore` 限制真正并发执行的预览生成；
- 计划提交使用非阻塞 `Lock`，竞争失败立即返回 `E_STAGE_BUSY`；
- 提交开始会递增 generation epoch，任何提交前捕获到旧 epoch 的生成结果都返回 `E_STALE_SLICE`，不能写入新状态；
- 资源上限包括材质像素总量、单块预估驻留字节和并行切片数；
- 并行数超限且不允许降级时返回 `E_RESOURCE_LIMIT`，允许时改为单线程并标记降级类别。

## 9. 自检

`selfcheck` 组合四项检查：

1. 逐块缓存拼接与一次性整幅生成逐字节一致；
2. 每个就绪切片的缓存键、语义键、节点路径、通道摘要和接缝摘要可互相核对；
3. 最近修订的重算目标与复用目标无交集且并集为全部块；
4. 快照文件存在、可解析、组件与总校验和正确。

