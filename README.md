# 确定性纹样浮雕切片组件

首版可演示实现：固定种子生成本地纹样高度场，经方向光浮雕与整幅归一化得到灰度图；按配置网格切片，支持单块预览、缓存续跑、并发组装、原子持久化、版本归档，以及可处理中断和强制终止后的确定性恢复。

实现不使用 Docker、外部服务、网络素材或第三方图像库。样本完全由随工程交付脚本在本地生成。

## 快速开始

要求 Python 3.11+、NumPy。以下命令同时适用于 Windows `cmd` 和 Unix shell：

```bat
py -3.11 -m pip install -r requirements.txt
py -3.11 generate_samples.py --output samples
py -3.11 -m relief_tiles.cli validate --config samples/config.json
```

也可以执行 `py -3.11 -m pip install -e .` 安装为可编辑包，并使用 `relief-tiles` 命令；直接以 `python -m relief_tiles.cli` 运行无需安装本项目。

生成后的 `samples/` 包含：

- `config.json`：完整可渲染配置。
- `pattern.json`：种子、纹样倍频、光照与相机参数摘录。
- `reference_full.png`：整幅灰度参照图。
- `reference_gray.npy`：用于逐像素核验的整幅灰度数组。
- `reference_height.npy`：整幅高度场。
- `sample_manifest.json`：样本文件 SHA-256 与配置版本标识。

先预览任意一块，再补齐其余块：

```bat
py -3.11 -m relief_tiles.cli clean --state .relief_state --output out
py -3.11 -m relief_tiles.cli preview --config samples/config.json --state .relief_state --output out --row 1 --col 2
py -3.11 -m relief_tiles.cli preview --config samples/config.json --state .relief_state --output out --row 1 --col 2
py -3.11 -m relief_tiles.cli render --config samples/config.json --state .relief_state --output out --reference samples/reference_gray.npy
```

第二次 `preview` 的 JSON 中 `"cached": true`。`render` 会复用预览块，只补算缺失块，并写出：

```text
out/mosaic.png
out/mosaic.npy
out/slices/r00_c00.png ... r03_c03.png
out/report.json
```

一条命令演示：

```bat
py -3.11 demo.py --root demo_run --regenerate-samples
```

`demo.py` 会清理自己的 `demo_run/`，重新生成或复用样本，执行两次预览并完成整幅拼装。

## 工程结构

```text
relief_tiles/
  config.py       JSON 校验、规范化配置、版本摘要
  noise.py        全局坐标梯度噪声、整幅/分块高度场
  gray.py         方向光浮雕、整幅归一化、量化
  store.py        原子块文件、摘要、清单、版本归档
  renderer.py     预览、并发调度、恢复、拼装、诊断
  output.py       PNG/NPY/JSON 输出
  cli.py          命令行入口
  samples.py      固定种子样本生成
generate_samples.py
demo.py
verify_acceptance.py
tests/
```

## 核心口径

### 1. 全局坐标高度场

每个倍频使用经典 Perlin 风格梯度噪声。对整幅格点 `(x, y)`：

```text
sx = x * frequency / canvas_width
sy = y * frequency / canvas_height
```

梯度表固定为 16 个单位基向量，由种子执行确定性 Fisher-Yates 置换，并展开到配置的表大小（默认 256）。格点梯度索引由 `seed + octave + (ix, iy)` 的 64 位整数哈希决定。倍频按配置顺序逐项累加：

```text
height(x, y) = sum(amplitude[i] * perlin(x, y, frequency[i], seed, i))
```

因此任意格点只依赖整幅坐标、种子和倍频参数，不依赖块划分、数组切片、调用顺序或线程。

### 2. 方向光浮雕与整幅归一化

高度场梯度在整幅坐标上取相邻格点差分：

- 内部格点使用中心差分，分母为 2。
- 整幅最外边界使用单边差分，分母为 1。
- 分块边界向两侧读取真实相邻高度，不使用块内复制边界，也不从局部原点重新计算相位。

方向光使用配置中的单位向量 `(lx, ly, lz)`，曲面法向为：

```text
n = normalize((-emboss_strength * gx, -emboss_strength * gy, 1))
raw = dot(n, light)
```

整幅 `raw` 的最小/最大值是唯一归一化权重。归一化后按 `quantization_levels` 量化，并映射到配置的 `[normalization_min, normalization_max]`，输出为 `uint16`。

### 3. 分块光晕

输出块 `[x0, x0+bw) x [y0, y0+bh)` 的灰度计算需要高度的真实左/右/上/下邻点，因此块计算窗口为：

```text
x: [max(x0 - 1, 0), min(x0 + bw, W - 1)]
y: [max(y0 - 1, 0), min(y0 + bh, H - 1)]
```

持久化只保存块核心 `bw x bh` 灰度，不保存光晕。块内容只由整幅偏移、覆盖范围和配置决定。

## 缓存、版本与恢复

### 原子块文件

每块是一个 `.npz`，包含：

- `gray`：`uint16` 块像素。
- `metadata`：版本标识、块行列、整幅偏移 `x0/y0`、覆盖宽高、声明像素数、dtype、SHA-256 内容摘要。

写入顺序为：同目录临时文件 → `flush + fsync` → `os.replace` 整体替换 → 原子更新清单。临时文件不会被识别为完成块。

### 版本标识

版本标识为规范化配置 JSON 的 SHA-256。状态目录结构：

```text
.relief_state/
  active_version.json
  versions/<version>/manifest.json
  versions/<version>/normalization.json
  versions/<version>/blocks/r0000_c0000.npz
  archive/<old-version>-<timestamp>/
```

重启时版本一致则逐块校验摘要并复用；版本变化时先把旧版本目录移动到 `archive/`，再启用新版本，不覆盖旧块。

### 可处理终止

`SIGINT`、`SIGTERM`、Windows `SIGBREAK` 只设置停止事件，不打断正在计算的块。调度器在当前块边界停止提交新块，等待已在途块原子落盘，然后：

1. 写 `.relief_state/interrupted.json`。
2. 以约定退出码 `75` 退出。

重新执行同一 `render` 命令即可复用已完成块并补齐缺失块。成功的完整渲染会删除旧中断标记。

### 强制终止

`SIGKILL` 或进程被直接结束时不承诺中断报告。重启只信任已原子替换且摘要有效的块；`.tmp-*` 临时文件会被清理，未完成块不会进入清单。补齐后的拼装结果仍与整幅直接生成逐像素一致。

## 并发与内存预算

`resources.concurrency_limit` 限制线程池并发上限。`resources.memory_budget_pixels` 按“在途块工作像素”计费：

```text
block_working_pixels = (block_width + 2) * (block_height + 2)
```

调度器按块行列索引序提交。下一个块会使在途像素超过预算时，后续块等待已有块完成；不会降低倍频、归一化或量化精度。配置预算连一个块都容纳不下时返回 `E_BUDGET`。

## 摘要失效与诊断报告

以下情况均判定块失效并记录 `E_DIGEST` 诊断，随后回退补算：

- 块文件缺失但清单声明其已完成。
- `.npz` 损坏或不可读。
- 版本、偏移、覆盖范围、dtype 或形状不符。
- 声明像素数与实际像素数不符。
- 内容摘要不匹配。
- 清单摘要与块文件不一致。

单块失效会被自动修复；若修复后仍不可用，命令以 `E_DIGEST` 失败。`out/report.json` 包含：

- `blocks`：逐块状态、摘要、是否复用/重算、失效理由。
- `invalid_blocks`：每个失效块的 `E_DIGEST` 原因和明细。
- `recomputed_blocks`、`reused_blocks`、`total_blocks`。
- `seam_max_abs_adjacent_delta`：所有块边界相邻像素灰度差最大值。
- `vertical_/horizontal_seam_max_abs_adjacent_delta`：分方向接缝值。
- `max_abs_diff_vs_full_reference`：与整幅参照的最大逐像素差。
- `first_mismatch`：若不一致，给出整幅 `x/y`、所属块、通道、实际值和参照值。
- `peak_in_flight_pixels`：实际调度峰值。

## 配置字段

示例见 `samples/config.json`。关键字段：

| 字段 | 约束 |
| --- | --- |
| `canvas.width/height` | 正整数 |
| `grid.rows/cols` | 正整数，且能整除画布 |
| `seed` | 非负 64 位整数 |
| `pattern.octaves` | 非空；频率严格递增且为正；振幅为正 |
| `pattern.gradient_table_size` | 2 的幂，默认 256 |
| `light.direction` | 三元单位向量，误差不超过 `1e-6` |
| `emboss.strength` | 正数 |
| `emboss.normalization_min/max` | 整数，`max-min>0`，范围适配 `uint16` |
| `emboss.quantization_levels` | `>=2`，且可由归一化范围表示 |
| `camera` | 方位角、俯仰角 `[-90,90]`、正距离；解析并纳入版本摘要 |
| `slice.preview_row/col` | 在网格内 |
| `resources.concurrency_limit` | 正整数 |
| `resources.memory_budget_pixels` | 正整数，至少容纳一个块 |

相机参数当前用于样本描述、校验与版本一致性；像素着色方向由显式 `light.direction` 决定。

## 业务错误码

| 错误码 | 含义 |
| --- | --- |
| `E_CONFIG_IO` / `E_CONFIG_JSON` / `E_CONFIG_VERSION` | 配置读取、JSON 或 schema 错误 |
| `E_CANVAS` / `E_GRID` | 画布或网格非法 |
| `E_SEED` | 种子非法 |
| `E_OCTAVE` / `E_FREQUENCY` / `E_AMPLITUDE` | 倍频表非法 |
| `E_LIGHT` / `E_EMBOSS` / `E_CAMERA` | 光照、浮雕、相机非法 |
| `E_NORMALIZATION` / `E_QUANTIZATION` | 归一化区间或量化级数非法 |
| `E_RESOURCE` / `E_BUDGET` | 资源上限非法 |
| `E_BLOCK_RANGE` | 请求块越界 |
| `E_DIGEST` | 块摘要/结构失效且无法恢复 |
| `E_MISMATCH` | 拼装图与整幅参照不一致 |
| `E_RENDER` | 块计算异常 |
| `E_STATE` / `E_REFERENCE` | 状态目录或参照文件错误 |
| `E_INTERRUPTED` | 在块边界按约定中断，退出码 75 |

配置任一字段非法时，命令只输出 JSON 错误，不创建状态目录，也不写出任何切片。

## 验证

完整自动化测试：

```bat
py -3.11 -m unittest discover -v
```

端到端验收（预览、整幅一致性、摘要篡改恢复、接缝报告）：

```bat
py -3.11 verify_acceptance.py --root acceptance_run
```

中断边界与 SIGKILL 恢复包含在 `tests/test_signals.py`。实际执行结果记录见 `VERIFICATION.md`。
