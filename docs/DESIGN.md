# Design Notes

## 1. Global-Coordinate Pattern

每个像素使用整幅坐标 `(x, y)`，而不是块内坐标。第 `k` 个倍频的频率为 `f_k`、振幅为 `a_k`，采样坐标为：

```text
u = x * f_k
v = y * f_k
```

整数格点上的单位梯度向量来自固定种子生成的 256 项梯度表。格点索引由稳定的 `splitmix64` 风格整数混合函数从 `(ix, iy, seed, octave)` 得到，不使用 Python 的随机化 `hash()`。

每个倍频使用 smoothstep 插值，最终高度为：

```text
H(x, y) = sum_k a_k * noise_k(x * f_k, y * f_k) / sum_k a_k
```

因此任意点高度只依赖该点坐标、种子与倍频参数；改变块网格、调用顺序或先算哪块都不会改变结果。

## 2. Directional Light and Global Normalization

对整幅高度场求相邻格点差分。内部像素使用中心差分，画布边界使用单边差分：

```text
gx = (H[y, min(x+1, W-1)] - H[y, max(x-1, 0)])
     / ((x > 0) + (x < W-1))

gy = (H[min(y+1, H-1), x] - H[max(y-1, 0), x])
     / ((y > 0) + (y < H-1))

raw = lz - relief_strength * (gx * lx + gy * ly)
```

`raw` 的全局最小/最大值由流式整幅扫描得到，并持久化到 `manifest.json`。量化公式：

```text
gray = clip(floor((raw - global_min) / (global_max - global_min) * 255 + 0.5), 0, 255)
```

块渲染会额外读取上下左右各 1 像素的全局邻域。块边界的中心差分因此取到邻块真值；块内绝不重新计算局部 `min/max`。

## 3. Preview and Cache

`preview_tile(row, col)`：

1. 若 manifest 中已有全局统计，直接读取；否则流式扫描一次全幅统计。
2. 若该块缓存版本一致且摘要有效，直接复用。
3. 否则只渲染该块并原子写入缓存。

首次预览的成本包含一次全幅统计扫描。之后无论单块预览还是完整渲染，都不会退化为整幅重算；全缓存重跑时仅做摘要校验、拼装和整幅参考比对。

## 4. Tile File Format and Atomicity

```text
8 bytes   magic "RLFTIL01"
4 bytes   little-endian JSON header length
N bytes   JSON header
M bytes   uint8 pixel payload, row-major
```

头包含：

- schema 与配置版本标识；
- `row`, `col`；
- 整幅坐标偏移 `origin_x`, `origin_y`；
- 覆盖范围 `width`, `height`；
- 声明像素数、dtype；
- 像素内容 SHA-256。

写入流程为：同目录临时文件 → 写入 → `flush` → `fsync` → `os.replace` → 目录 `fsync`（POSIX）。进程被强杀时最多留下临时文件；重启清理临时文件，只信任完整且摘要有效的块。

配置版本是规范化 JSON 的 SHA-256。manifest 版本不一致时，状态目录所有块和旧输出整体作废，不允许新旧混拼。

## 5. Concurrency and Memory Budget

主线程维护最多 `concurrency` 个在途 `Future`。切片按行优先、列优先排序提交。每个切片估算工作内存：

```text
tile_cost = tile_width * tile_height * 80 bytes
```

该估算覆盖高度块、邻域切片、梯度、原始光照和输出缓冲。若下一个块会超过内存预算，调度器停止提交并等待前面的块完成；不会跳块、降倍频或降低归一化精度。单块自身超过预算时返回 `E_MEMORY_BUDGET`。

任一分块抛错后停止提交、取消待处理块，等待已运行块到达边界后返回首个错误。并发修改不影响像素结果，因为每个块只读取不可变配置、全局统计与自身坐标。

## 6. Digest Verification and Repair

所有块完成后逐块重读缓存：

- 文件缺失；
- 魔数/头错误；
- 版本或偏移/覆盖范围不匹配；
- 声明像素数与实际 payload 长度不一致；
- 内容 SHA-256 不匹配；

任一情况记为该块失效，并写入 `E_DIGEST` 事件，删除该块后重新渲染。补算后再次全量校验；仍失败才抛出不可恢复的 `E_DIGEST` 错误。

拼接完成后直接生成同参数整幅参考图，按全局行优先顺序找首个误差超过容差的像素。报告包含：

```text
x, y, tile(row/col/offset), assembled, reference, abs_error, cause
```

残差分类会额外计算三种诊断变体：块内归一化、块内梯度、块内坐标相位，以区分 `local_normalization`、`local_gradient_boundary`、`local_coordinate_phase`、`tile_boundary_phase_or_gradient` 或内容/元数据问题。

## 7. Interruption Boundaries

CLI 将 `SIGINT`、`SIGTERM`（Windows 下还包括 `SIGBREAK`）转为引擎终止事件。引擎只在分块边界：

1. 停止提交新块；
2. 等待已运行块完成并原子持久化；
3. 校验当前完整块；
4. 原子写 `interrupted.json`，列明 signal、退出码、已完成块和剩余块；
5. 以约定退出码退出。

强制终止不做清理承诺。重启时只读取完整且摘要有效的 `.tile` 文件，因此强杀和可处理终止最终生成同一个逐像素结果。

## 8. Desktop Diagnostics

PySide6 视图提供：

- 整幅浮雕灰度图；
- 可开关的切片网格与接缝高亮；
- 当前块黄色边框与像素十字标记；
- 块列表，双击跳转到对应块中心；
- 一致性表，点击 `E_CONSISTENCY`/`E_DIGEST` 记录跳转到对应块和整幅坐标。

生成任务在线程中执行，UI 不阻塞。渲染完成后从输出 manifest 重建块表。
