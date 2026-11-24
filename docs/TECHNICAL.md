# 技术说明与复核口径

## 1. 坐标与网目相位

岗位的 `lpi` 是每英寸网线数，`dpi` 是输出像素每英寸数，因此网目间距为：

```text
p = dpi / lpi           # 单位：输出像素/网目周期
f0 = 1 / p = lpi / dpi  # 单位：周期/像素
```

像素中心使用整幅输出坐标 `(x, y)`，从 `(0, 0)` 开始：

```text
X = x + 0.5
Y = y + 0.5
θ = π * angle_deg / 180

u =  X cosθ + Y sinθ
v = -X sinθ + Y cosθ
```

`(u, v)` 是旋转后的网目坐标。单元内居中相位为：

```text
α = (u / p) - floor(u / p) - 0.5
β = (v / p) - floor(v / p) - 0.5
```

因此 `α, β ∈ [-0.5, 0.5)`。相位只由整幅像素坐标、`angle_deg`、`dpi` 和 `lpi` 决定；分块函数接收全局 `origin_x/origin_y`，不会把块内 `(0,0)` 当网角原点。对同一全局像素，无论从整幅数组、块数组还是 `1×1` 数组调用，公式和浮点输入均相同，结果逐像素一致。

半开区间 `[x0, x1)`、`[y0, y1)` 是唯一分块所有权口径：相邻块不重复写同一像素，但缝两侧都代入同一整幅公式。若外部系统采用包含边界的重叠块，重叠坐标也会得到相同值。

## 2. 形状、覆盖上限和量化

令归一化灰度为 `g = gray / 255`，其中 `0` 为黑。油墨需求和覆盖截断为：

```text
tone   = 1 - g
c      = min(tone, ink_coverage_max)
```

面积校准的相位统计量 `A(α, β)` 为：

```text
square:  A = 4 max(|α|, |β|)^2
circle:  A = π(α² + β²)
ellipse: A = π((α/r)² + (rβ)²),  r = ellipse_aspect
```

边界满足 `A = c`，也可写成乘积/比例裁决 `A / c <= 1`。方形的 `4m² <= c` 对应半边长 `sqrt(c)/2`、面积 `c`；圆形的 `π(α²+β²) <= c` 对应半径 `sqrt(c/π)`。

为了让有限级岗位产生可量化的抗锯齿边缘，使用固定宽度的确定性过渡：

```text
a = clip(0.5 + (c + 1e-12 - A) / 0.18, 0, 1)
c = 0 的像素强制 a = 0
k = clip(floor(a * (levels - 1) + 0.5), 0, levels - 1)
D = 255 - round(255 * k / (levels - 1))
```

`D` 是写入 PNG 和块摘要的设备灰度：`255` 为纸张白，`0` 为满墨。截断发生在几何覆盖量上，量化发生在最终网点 alpha 上；全链路只有这一套 `floor(x+0.5)` 最近级口径。`levels=2` 时输出只含 `0/255`。

## 3. 跨分辨率有理数重采样

设原图宽高为 `(sw, sh)`，输出为 `(ow, oh)`。像素中心映射到源图：

```text
sx = (ox + 0.5) * sw / ow - 0.5
sy = (oy + 0.5) * sh / oh - 0.5
```

实现把 `sx` 写成精确整数有理数：

```text
num_x = (2ox + 1) * sw - ow
den_x = 2ow
sx = num_x / den_x
```

随后用整数 floor 得到相邻源像素，用余数得到双线性权重；越界索引夹到边缘像素。结果再用 `floor(value + 0.5)` 确定为 `uint8`。同一输入、岗位和 NumPy 版本下结果确定，不依赖线程或分块。

## 4. 整幅同口径裁决

所有块完成后，引擎从状态目录重新读取并验证块，按 `(offset_x, offset_y)` 装入 `int16` 整幅画布，缺失位置保留 `-1`。同时调用同一个 `render_halftone(output_gray, job, 0, 0)` 生成整幅真值。

发现不一致时按 C 顺序的第一个差异返回：

```json
{
  "code": "E_CONSIST",
  "details": {
    "first_mismatch": {
      "x": 13,
      "y": 9,
      "tile_x": 0,
      "tile_y": 0,
      "tile_index": 0,
      "tile_value": 255,
      "full_value": 0
    }
  }
}
```

`python -m halftone consistency-demo` 会在内存中故意改写一个像素并打印该结构，不写出网点图。

## 5. 摩尔纹频谱指标

只截取岗位的 `valid_region = [x,y,w,h]`；未声明时为整幅。将设备灰度转成墨量 `(255-D)/255`，去均值并加二维 Hann 窗，然后计算 `fft2` 功率谱。

挂网基频点为：

```text
( fx, fy ) = ( ±f0 cosθ, ±f0 sinθ )
以及旋转 90° 的另一组正交基频点
```

邻域半径取：

```text
r_nbhd = max(0.16 f0, 2 max(1/w, 1/h))
```

指标为四个基频邻域内功率和除以全频谱功率和：

```text
M = Σ P(邻域) / Σ P
```

报告同时给出 `risk_index`、`moire_risk_index`、`threshold`、`exceeds_threshold`、有效区域和有效像素数。该指标越高表示能量越集中在挂网频率邻域；是否判为风险由岗位配置阈值决定。

## 6. 版本标识与原子状态

原图内容摘要不是文件修改时间或文件名，而是规范化的像素内容：

```text
source_digest =
  SHA256("uint8:<width>x<height>\n" + contiguous_uint8_pixels)
```

岗位摘要来自 `ValidatedJob.canonical_dict()` 的排序键紧凑 JSON。版本标识为：

```text
version_id = SHA256(
  "halftone-version-v1\n" +
  SHA256(canonical_job_json) + "\n" +
  source_digest
)
```

每个块文件包含：

- `index/tile_x/tile_y/offset_x/offset_y/width/height`
- `full_width/full_height/tile_width/tile_height`
- `version_id/job_id`
- 原始 `uint8` 像素的 base64
- `pixel_sha256 = SHA256(raw_pixel_bytes)`
- `content_sha256 = SHA256(canonical_json(除 content_sha256 外的全部字段))`

写入时先在同一目录用 `tempfile.mkstemp` 建临时文件，写入、flush、fsync 后 `os.replace` 到正式块文件。报告、PNG、JSON 和切片 ZIP 也使用同目录临时文件加 `os.replace`。

恢复协议：

1. manifest 的 `version_id` 相同：扫描所有正式 `tile_*.json`，重算摘要、像素摘要、版本和几何；有效块复用，缺失块补算。
2. `version_id` 不同：先把旧 manifest 和 `tiles/` 移到 `archive/<old_version>[-rNNN]/`，再写新 manifest 和新块；不会直接覆盖旧块。
3. 正式块摘要损坏：返回 `E_DIGEST`，岗位不导出最终图；异常清单把该码标记为预期注入结果。
4. 强杀发生在临时文件阶段时，正式文件不存在或仍是上一个完整版本；`.` 开头的 `.tmp` 不匹配 `tile_*.json`，不会被当作有效块。

## 7. 并发、内存和终止

- 岗位按 `job_id` 字典序处理；岗位内块按行主序 `index = tile_y * tile_cols + tile_x`。
- 每个岗位使用 `ThreadPoolExecutor(max_workers=workers)`，因此同时运行的块不超过并发上限。
- `MemoryBudget` 以 `tile_width * tile_height * uint8.itemsize` 预估每块缓冲。调度线程按索引顺序 `acquire()`；预算不足时在条件变量上等待，已完成块的 worker 在 `finally` 中释放预算。
- 不会因为预算紧张而改变 `levels`、`dpi`、`lpi` 或相位精度。
- `SIGTERM/SIGINT/SIGBREAK` 处理函数只设置 `TerminationToken`。调度器在提交新块前和获取预算等待中检查令牌；已经运行的块会完成并原子落盘，然后不再提交后续块。CLI 返回 `20`。
- `--term-after-tiles N` 是确定性测试钩子，在 N 个新块完成后请求终止。

## 8. 异常注入

`samples/anomalies.json` 中四项分别从合法基座岗位深拷贝后施加：

- `angle_deg = 95` → `E_ANGLE`
- `tile_cols = 7`（320 不能被 7 整除）→ `E_GRID`
- 删除 `dot_shape/shape` → `E_SHAPE`
- 先真实生成并持久化第 0 块，再翻转 `content_sha256` 首字符 → `E_DIGEST`

批处理捕获岗位级 `BusinessError`，继续其余岗位。最终 `out/report.json` 汇总 `injection_results`、实际码、是否匹配、`successful_jobs` 和 `failed_jobs`。失败岗位不会执行最终 PNG/ZIP 导出。
