# 装配示意图的确定性泊松融合

本项目实现一条可直接运行的泊松图像融合流水线，面向“零件渲染图嵌入装配底板”的评审场景。项目只依赖 Python 3 与 NumPy，不依赖网络、外部图片或额外图像库；PNG 编解码、固定种子样本生成、梯度场构建、CG 求解、接缝报告、增量替换和运行日志均在仓库内实现。

## 快速开始

```bash
python3 -m pip install -r requirements.txt
python3 -m poisson_fusion demo --seed 20261128 --workdir demo_output
python3 -m unittest discover -s tests -v
```

`demo` 会依次完成：

1. 以固定种子生成底板、RGBA/纯色背景前景图、新旧掩码、边界环和边界带。
2. 执行全量泊松融合。
3. 使用新零件替换同掩码区域，并执行全量重算一致性核验。
4. 使用新零件与变化后的掩码再次替换，输出新旧掩码差集和重新求解说明。

最终摘要写入 `demo_output/demo_summary.json`，所有运行记录追加到 `runs/run_log.jsonl`。

## 命令

### 生成固定种子样本

```bash
python3 -m poisson_fusion generate \
  --seed 20261128 \
  --out samples \
  --height 192 --width 256 --band-radius 3
```

同一 `seed`、尺寸与边界带半径会生成逐字节相同的 `.npy` 数组。样本目录同时包含可直接查看的 PNG、`sample_metadata.json` 数组摘要，以及用于异常测试的 `invalid/` 输入。

### 全量融合

```bash
python3 -m poisson_fusion fuse \
  --base samples/base.npy \
  --foreground samples/foreground_rgba.npy \
  --mask samples/target_mask.npy \
  --output-dir runs/initial \
  --band-radius 3 \
  --tolerance 1e-8 \
  --max-iterations 8000
```

也支持 `--foreground-mode solid`（或用默认 `auto` 自动识别纯色背景），以及 `--background-color R,G,B`。输入既可为 `.npy`/`.npz`，也可为 8 位非隔行灰度/RGB/RGBA PNG。

### 增量替换

```bash
python3 -m poisson_fusion replace \
  --state runs/initial/state.json \
  --foreground samples/foreground_v2_rgba.npy \
  --output-dir runs/replace_same_mask

python3 -m poisson_fusion replace \
  --state runs/initial/state.json \
  --foreground samples/foreground_v2_rgba.npy \
  --mask samples/target_mask_v2.npy \
  --output-dir runs/replace_changed_mask
```

替换时实际只求解“旧掩码 ∪ 新掩码”的包围盒（外加 2 像素数值余量）；同时仍执行一次全量重算用于逐像素一致性核验。若增量结果与全量结果的最大通道差超过 1 个量化级，程序返回 `E_STATE` 且不输出融合图。

## 求解定义

- 掩码 `M` 是单连通、闭合且不接触图像边缘的融合区域。
- 一像素内边界 `∂M` 固定为底板 `B` 的颜色，因此输出在掩码边界上与底板逐像素一致。
- 内部区域 `Ω = M \ ∂M` 求解离散负拉普拉斯方程：

```text
4F(x,y) - F(x-1,y) - F(x+1,y) - F(x,y-1) - F(x,y+1)
    = -div(g)(x,y) + 已知内边界邻居贡献
```

- `gx(x,y) = Fg(x+1,y) - Fg(x,y)`，`gy(x,y) = Fg(x,y+1) - Fg(x,y)`，其中 `Fg` 是提取有效零件像素后、用最近有效像素补齐透明/纯色背景得到的前景图。
- 求解器为 Jacobi 预条件共轭梯度。收敛条件为：

```text
max_abs_residual <= tolerance * max(1, max_abs_rhs)
```

- 每个颜色通道的迭代次数、残差、RHS 尺度和是否收敛均写入 `report.json`。不收敛时报错包含通道、迭代次数、最大残差位置；出现 NaN/Inf 时报错包含首个非有限像素和当前迭代信息，且不会写出融合图。

边界带定义为内边界的一像素边界环经曼哈顿距离扩张 `band_radius` 像素后的区域，同时覆盖掩码内外。

## 增量与掩码变化语义

令旧掩码为 `M_old`，新掩码为 `M_new`：

- 更新区域：`M_old ∪ M_new`。
- `M_old \ M_new`：旧零件独占区，恢复为底板像素。
- `M_new \ M_old`：新零件新增区，使用新前景梯度重新求解。
- `M_old ∩ M_new`：公共区因前景梯度变化而重新求解。
- `(M_old ∪ M_new)` 之外的像素：直接复用上一次融合结果，核验报告记录是否逐位保持一致。

当掩码变化时，输出 `mask_old_only`、`mask_new_only`、`mask_change_xor`、`mask_common` 的 `.npy` 与 `.png`，并在 `report.json.mask_change` 中记录像素数、包围盒和处理策略。

## 输出文件

每次运行目录中的主要文件：

| 文件 | 内容 |
| --- | --- |
| `fused.png` / `fused_u8.npy` | 可直接查看的融合图与精确 8 位数组 |
| `fused_float.npy` | 求解后的浮点融合结果 |
| `report.json` | 输入摘要、求解信息、边界指标、更新区域、掩码差集与一致性核验 |
| `state.json` | 下一次增量替换所需状态 |
| `seam_error_map.npy` / `seam_error_heatmap.png` | 逐像素最大 RGB 接缝误差与热图 |
| `seam_error_rgb.npy` | 逐像素、逐通道绝对误差 |
| `gradient_error_map.npy` / `gradient_error_heatmap.png` | 逐像素前景梯度 RMS 误差 |
| `boundary_band.npy` / `boundary_band.png` | 边界带状区域标注 |
| `boundary_mask.npy` / `boundary_mask.png` | 掩码内边界环 |
| `updated_region_mask.npy` / `updated_region.png` | 本次更新区域 |
| `updated_region_overlay.png` | 更新区域叠加标注 |
| `fields/fields_manifest.json` | 边界条件与梯度场的可序列化清单 |
| `fields/*.npy` | 边界 RGB、梯度 `gx/gy`、有效像素、未知区等数组 |

替换运行还额外输出：

| 文件 | 内容 |
| --- | --- |
| `full_recompute.png` / `full_recompute_u8.npy` | 用于核验的全量重算图 |
| `incremental_full_diff.npy` | 增量结果与全量结果的逐像素逐通道差 |
| `incremental_full_diff_heatmap.png` | 差异热图 |
| `mask_old*` / `mask_new*` | 替换前后掩码 |

误差报告中的所有最大值、均值与分位数均直接由保存的 `float32` 误差图逐像素计算，避免报告与误差图不一致。

## 业务错误码

| 错误码 | 数值码 | 含义 | 进程退出码 |
| --- | ---: | --- | ---: |
| `E_IMAGE_IO` | 1001 | 文件无法读取/写入 | 10 |
| `E_DIMENSIONS` | 1101 | 前景图、底板、掩码尺寸不一致 | 11 |
| `E_INVALID_IMAGE` | 1201 | 图像格式或数组形状无效 | 12 |
| `E_NONFINITE_INPUT` | 1202 | 输入包含 NaN/Inf | 12 |
| `E_EMPTY_MASK` | 2001 | 掩码为空 | 21 |
| `E_FULL_MASK` | 2002 | 掩码覆盖整幅图像 | 22 |
| `E_BROKEN_BOUNDARY` | 2003 | 边界断裂、接触图像边缘或非单连通区域 | 23 |
| `E_MASK_SHAPE` | 2004 | 掩码不是二维数组 | 24 |
| `E_FOREGROUND_EXTRACTION` | 2101 | 无法提取有效零件像素 | 25 |
| `E_SOLVER_NOT_CONVERGED` | 3001 | 未在容差内收敛 | 31 |
| `E_SOLVER_NONFINITE` | 3002 | 求解过程中出现非有限值 | 32 |
| `E_STATE` | 4001 | 增量状态无效或一致性核验失败 | 41 |

失败时错误以 JSON 写入目标目录的 `error.json`，同时记录到运行日志；求解失败不会生成 `fused.png`。

## 验证要点

- 样本生成逐字节可复现。
- 融合图在掩码内边界与底板逐像素一致。
- `report.json` 的边界误差统计与 `seam_error_map.npy`、`gradient_error_map.npy` 一致。
- 增量结果与全量重算最大通道差不超过 1；固定种子样例中通常为 0。
- 更新区域之外复用旧像素，核验字段 `unaffected_pixels_exactly_preserved` 为 `true`。
- 同输入重复全量运行会生成相同融合数组与相同 `run_id`/指标。
- 运行日志以追加 JSON Lines 方式记录输入摘要、关键指标和输出路径。
