# 钟表机芯半色调教学展具内核

本项目用纯 Python + NumPy 实现一套本机可重复运行的印前半色调内核，面向擒纵轮、摆轮游丝、齿轮系与轴心圆孔的组合教学看板。固定种子生成器不读取任何外部图像；所有快照、误差报告、频谱归因和局部重算差异均可复现。

## 快速开始

```bash
python3 -m pip install -r requirements.txt

# 生成内核示意样本（灰阶、零件掩码、细节真值、PGM 预览）
python3 main.py generate --seed 20261128 --size 512

# 输入解析与业务校验
python3 main.py inspect \
  --sample samples/watch_movement_seed_20261128 \
  --config configs/default.json

# 50 LPI 全量快照
python3 main.py run \
  --sample samples/watch_movement_seed_20261128 \
  --config configs/default.json \
  --out snapshots/screen_50lpi_default_density \
  --snapshot-id watch_seed_20261128_50lpi_default

# 单零件密度修改与局部重算，同时做全量重算逐像素核验
python3 main.py recompute \
  --sample samples/watch_movement_seed_20261128 \
  --config configs/default.json \
  --snapshot snapshots/screen_50lpi_default_density \
  --part 1 --density 0.615 \
  --out snapshots/local_recompute_escape_density_boost

# 一键生成样本、50/75 LPI 两档快照和一次局部重算
python3 main.py demo
```

运行测试：

```bash
python3 -m unittest discover -v
```

当前 14 项测试覆盖：生成器字节级确定性、输入错误码、抖动纹理幅度、自适应核可复现、残差守恒、密度校准、色调/频谱/细节报告，以及局部重算与全量重算逐像素相等。

## 数据契约

样本目录包含：

```text
samples/watch_movement_seed_20261128/
  sample.json          # 样本清单、文件哈希、标签集合
  gray.npy             # uint8，形状 (H, W)，0..255
  mask.npy             # int16，0=背景，1..4=零件
  details.json         # 零件纹理、目标密度、细节 bbox 与构造真值
  gray_preview.pgm     # 8 位 P5 灰度预览
  mask_preview.pgm     # 掩码预览
```

`details.json` 的四类零件：

| 标签 | 名称 | 纹理类别 | 重点核验 |
|---|---|---|---|
| 1 | `escape_wheel` | `tooth` | 擒纵轮齿根/齿顶 |
| 2 | `balance_hairspring` | `spiral` | 游丝连续色调与螺旋周期 |
| 3 | `gear_train` | `tooth` | 大齿轮与小齿轮齿廓 |
| 4 | `arbor_bore` | `bore` | 轴心圆孔开闭 |

每个 `detail_regions[]` 带 `bbox`、`kind`、`center`、半径/齿数、`isolation_radius_px` 和 `truth`。分析时按 `isolation_radius_px` 隔离同标签的邻近齿轮，避免同零件不同结构互相污染。

## 输入校验与打印栅格

`inspect` 会校验：

- 图像二维尺寸、8 位位深、掩码 dtype 与尺寸。
- `mask.labels`、`mask.part_labels`、实际标签集合互相一致。
- `-1` 缺失像素、未登记标签、声明但无像素的零件。
- 细节区域越界、未覆盖所属零件、轴心区域缺圆心。
- 展板 DPI、像素纵横比、打印线数、网点节距、墨点直径。

打印栅格推导：

```text
dpi_x = image_width_px / (board_width_mm / 25.4)
dpi_y = image_height_px / (board_height_mm / 25.4)
cell_pitch_x_px = dpi_x / lines_per_inch
cell_pitch_y_px = dpi_y / lines_per_inch
dot_diameter_px = ink_dot_diameter_mm / 25.4 * mean(dpi_x, dpi_y)
```

解析清单包含每个零件的 256 桶灰度直方图、面积占比、bbox、按面积可容纳网点单元数、按 bbox 可容纳单元数及墨点面积占单元比例。

### 稳定业务错误码

| 错误码 | 含义 |
|---|---|
| `E_IMAGE_SIZE` | 灰阶尺寸与元数据不一致 |
| `E_GRAY_DEPTH` | 灰阶不是受支持的 8 位矩阵 |
| `E_MASK_SIZE` | 掩码与灰阶尺寸不一致 |
| `E_MASK_UNKNOWN_LABEL` | 出现未登记标签 |
| `E_MASK_LABEL_MISSING` | 声明零件没有像素 |
| `E_MASK_MISSING_PIXELS` | 存在 `-1` 缺失像素，或细节区域未落在零件上 |
| `E_INPUT_HASH` | 启用哈希核验时输入文件与清单不一致 |
| `E_PRINT_LPI` | 打印线数导致栅格不足 1 像素 |
| `E_PRINT_DOT_SIZE` | 墨点直径超过网点节距 |
| `E_PRINT_GEOMETRY` | 展板物理尺寸或像素纵横比不相容 |
| `E_DETAIL_REGION` | 细节区域越界或元数据无效 |
| `E_DENSITY_BOUNDS` | 目标密度或修正量越界 |
| `E_SNAPSHOT_NOT_FOUND` | 快照或状态文件缺失/不匹配 |
| `E_RECOMPUTE_PART` | 局部重算零件标签无效 |
| `E_RECOMPUTE_VERIFY` | 局部重算与全量重算不一致 |

CLI 以 JSON 输出错误对象并以退出码 `2` 结束。

## 半色调算法

1. **确定性抖动**：阈值偏移由 `splitmix64(x, y, seed, channel)` 生成，不依赖全局随机数状态；幅度按零件的 `texture_class` 取 `flat / spiral / tooth / bore`。平坦底纹得到较大随机阈值，游丝得到较小幅度以保持平滑渐变。
2. **梯度自适应核**：用原始灰度计算梯度幅值与法向，切向方向划分 8 个 45° 区，并对中间/强梯度各构造一种方向核；弱梯度使用各向同性 Floyd-Steinberg 核。核选择逐像素记录为 `kernel_class.npy`。
3. **蛇形逐行扫描**：奇数行反向，当前行只向右/向左的前方像素以及下一行尚未处理的像素分配残差。核定义、行扫描方向和邻域顺序完全一致。
4. **精确残差分配**：`r = adjusted_tone + arrived_error - out`。可用邻域权重和不足 1 时只对可用同标签邻域重归一化，并记录裁剪质量；没有可用邻域时记入 `unallocated_residual.npy`。全图检查 `requested - output - unallocated` 平衡。
5. **掩码感知扩散**：不把残差分配到另一个零件标签。掩码边界裁剪因此可量化，也使单零件修改不会扰动其他零件。

输出约定：

- `halftone.npy`：`1=墨点，0=纸白`。
- `halftone_preview.pgm`：`0=黑，255=白`，便于查看。
- `incoming_error.npy`：每像素量化前收到的累积误差。
- `quantization_error.npy`：每像素 `value - binary` 残差。
- `weight_clip_map.npy` / `unallocated_residual.npy`：裁剪权重与未分配残差定位。

## 密度修正

先以零修正跑一次基线，得到每个零件的实际二值网点密度；然后：

```text
correction = clip(target_density - baseline_density, -max_correction, +max_correction)
threshold_offset = -correction
effective_tone = continuous_ink - threshold_offset
```

误差扩散若只改阈值而不进入反馈输入，稳态平均密度基本不变，因此实现把阈值偏移作为等价色调偏移注入反馈环。报告同时给出基线密度、目标、实际修正后密度、阈值偏移、是否截断、打印单元容量和是否落入全黑/全白保护范围。

## 报告与快照

一次 `run` 输出：

```text
snapshot.json                     # 快照清单、目标、修正、哈希、报告状态
state.npz                         # 局部重算所需固化状态
halftone.npy / halftone_preview.pgm
incoming_error.npy / quantization_error.npy
weight_clip_map.npy / unallocated_residual.npy
dither_offsets.npy / threshold_offsets_per_pixel.npy / kernel_class.npy
reports/input_analysis.json       # 解析清单、直方图、栅格容量
reports/quantization_report.json  # 核、抖动、残差守恒、逐零件误差
reports/density_report.json       # 修正前后密度与容量
reports/tone_error_report.json    # 滑动窗口色调偏差与来源分量
reports/uniformity_report.json    # 频谱峰、齿形谐波、条带定位与归因
reports/detail_fidelity.json      # 齿根断裂、齿顶粘连、轴孔闭合、边缘偏移
reports/kernel_usage.json
parts/part_XX_name/               # 逐零件图、裁剪图、掩码裁剪、零件报告
```

### 色调误差

以掩码内滑动窗口比较连续墨量均值与二值网点占有率，报告均值偏差、最大偏差、超限窗口坐标。来源分量定义：

- `density_correction = baseline_window_tone - corrected_window_tone`
- `mask_boundary = (source - baseline) * 窗口内掩码边界像素比例`
- `kernel_clipping = (source - baseline) * 窗口内非边界比例`

同时记录窗口内 `weight_clip_mass` 与未分配残差，便于区分裁剪和掩码边界。

### 频谱与条带归因

- 对每个零件 ROI 加 Hann 窗后做 2D FFT，检测局部峰值并去除共轭半平面重复。
- 规则网点判定使用实际 `(fx, fy)` 到网点倒格点 `(m/cell_pitch_x, n/cell_pitch_y)` 的距离，而不是只看径向频率。
- 齿形周期使用细节真值中的 `tooth_period_px` 与中心角向 720 点采样谐波。
- 行/列均值剖面的高通残差做方向自相关；条带区间按 z 分数定位。
- 归因代码：`regular_screen_interference`、`tooth_period`、`error_diffusion_directional_bias`；无法匹配的谱峰列入 `unclassified`。

### 齿形细节

- 先按 `max(min_px, cell_pitch*2, dot_diameter*3)` 选择奇数平滑窗，再以平滑网点占有率重建实心轮廓。
- 小面积封闭孔洞被填充，大轴孔保留；边缘用近似欧氏倒角距离做双向偏移统计。
- 边缘容差 `max(min_px, dot_radius*edge_tolerance_dot_radii, 0.35*smoothing)`，墨点尺寸变化会改变平滑窗与归一化口径。
- 报告同时给原始像素偏移、按墨点直径/半径归一化偏移、p95 与最大偏移。
- 齿根断裂按根部环的显著空段计数；齿顶粘连按尖端/节圆环中实际齿段相对掩码参考齿段的缺失数计数；轴孔闭合按孔内平滑墨量阈值判定。

## 单零件局部重算

`recompute` 从快照读取：

- `binary`
- `incoming_error`
- `weight_clip_map`
- `unallocated_map`
- `dither`

只重算目标零件全部像素；重算保护域外直接保留快照状态。由于主算法不允许残差跨标签，目标零件的上游残差全部来自同标签内部，外部状态仍按快照固化。保护域是“目标零件 + 1 像素邻域”，报告列出全部变化像素、行区间、边界误差连续性，并可执行同参数全量重算逐像素对照。

验收样本上局部重算与全量重算：

```text
full_recompute_mismatch_pixels = 0
outside_scope_mismatch_pixels = 0
changed_outside_part = 0
pixel_exact = true
```

## 样本与快照清单

- `samples/catalog.json`：已生成样本清单。
- `snapshots/catalog.json`：50 LPI、75 LPI 快照和局部重算差异清单入口。
- `configs/default.json`：默认 50 LPI、0.15 mm 墨点、512×512 展板栅格。
- `configs/fine_75lpi.json`：75 LPI、0.12 mm 墨点及部分零件密度覆盖。
- `configs/coarse_40lpi.json`：40 LPI、0.19 mm 墨点示例。

同一输入、配置和种子下，`halftone.npy`、误差数组、报告 JSON 与 `state.npz` 均逐字节可复现；PNG/PGM 仅用于查看，不参与判定。
