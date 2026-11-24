# 半色调分块处理组件

面向游戏包装印刷与海报输出的本地确定性半色调组件：固定种子生成灰度样本，按岗位配置完成有理数重采样、整幅坐标挂网、有限级量化、分块持久化、整幅一致性裁决、摩尔纹诊断、异常注入验证和原子导出。

原始任务说明已存档在 `docs/original-task-spec.md`；公式、状态格式和调度设计见 `docs/TECHNICAL.md`；实际验证记录见 `docs/VERIFICATION.md`。

## 环境与快速开始

Windows PowerShell：

```powershell
python --version                 # 需 3.11+；实测 python 为 Python 3.13.14
python -m pip install "numpy>=1.26"
python -m halftone seed --root .
python -m unittest discover -s tests
python -m halftone run --root . --workers 2 --check
python -m halftone consistency-demo
```

也可一键执行 `tools/Verify-Halftone.ps1`。首次复现前确认 `samples/` 已由种子脚本生成，并保持 `state/`、`out/` 中只有 `.gitkeep`。

当前 WSL 工作区的默认 `python` 是 3.10.12 + NumPy 1.26.4；代码使用 `from __future__ import annotations`，同时在 Windows `D:\Python3.13\python.exe`（3.13.14 + NumPy 2.5.2）下通过全部测试。Windows 侧另装有 uv 管理的 CPython 3.11.16。

## 常用命令

```bash
# 生成/强制重建固定种子样本
python -m halftone seed --root .
python -m halftone seed --root . --force

# 运行 5 个正常岗位 + 4 个异常注入岗位
python -m halftone run --root . --workers 4 --check

# 不带异常注入，便于终止/恢复演练
python -m halftone run --no-inject --workers 1 --term-after-tiles 2
echo $?                          # 约定退出码 20
python -m halftone run --no-inject --workers 4 --check

# 校验单个岗位 JSON
python -m halftone validate --job path/to/job.json

# 查看 E_CONSIST 首不一致像素结构
python -m halftone consistency-demo
```

## 输入与输出

- `samples/images.json`：灰度原图清单，包含尺寸和规范化内容摘要。
- `samples/jobs.json`：5 个覆盖圆形、椭圆、方形、二值/有限级和跨分辨率的岗位。
- `samples/anomalies.json`：非法网角、分块不整除、缺失形状、损坏块摘要四类注入。
- `state/<job_id>/manifest.json`：岗位版本和分块网格清单。
- `state/<job_id>/tiles/tile_*.json`：原子写入的原始像素 base64 块、偏移、范围和双层 SHA-256 摘要。
- `state/<job_id>/archive/<old_version>/`：版本变化时归档的旧 manifest 与分块。
- `out/<job_id>.png`：规范 8 位灰度网点图，`0` 为满墨、`255` 为纸张白。
- `out/<job_id>_ink.png`：使用岗位油墨色调生成的 RGB 预览。
- `out/<job_id>_tiles.zip`：全部切片 PNG 的原子打包产物。
- `out/report.json`：岗位结果、版本、复用/重算块数、摩尔纹指标、注入码和资源峰值。

## 岗位字段

| 字段 | 校验/含义 |
| --- | --- |
| `job_id` | 安全文件名标识：字母数字、`_`、`.`、`-` |
| `source_id` | 必须出现在 `samples/images.json` |
| `output_width` / `output_height` | 正整数；也支持 `resolution: 512` 或 `output_resolution` |
| `dpi` | 有限正数，默认 300；`pitch_px = dpi / lpi` |
| `lpi` | 有限正数，支持 `screen_lpi` / `line_lpi` |
| `angle_deg` | 有限数且 `[0, 90)`，支持 `screen_angle` |
| `dot_shape` | 必填，允许 `circle`、`ellipse`、`square` |
| `ink_tone` | `#RRGGBB` 或 `[r,g,b]`，用于油墨预览 |
| `ink_coverage_max` | 有限数且 `[0,1]` |
| `tile_cols` / `tile_rows` | 正整数，列数整除宽、行数整除高 |
| `levels` | `[2,256]` 的整数，默认 2（二值） |
| `valid_region` | 可选 `[x,y,w,h]`，摩尔纹频谱只统计该有效矩形 |
| `moire_threshold` | `(0,1]`，岗位级频谱聚集度阈值 |

## 业务码

| 业务码 | 触发场景 |
| --- | --- |
| `E_CONFIG` / `E_MANIFEST` | JSON、岗位 ID、清单或通用参数不合法 |
| `E_RESOLUTION` | 输出分辨率/DPI 非正整数或非有限正数 |
| `E_LPI` | 挂网线数缺失、非有限数或非正数 |
| `E_ANGLE` | 网角不在 `[0,90)` |
| `E_SHAPE` | 缺失或不支持的网点形状 |
| `E_COVERAGE` | 油墨覆盖上限不在 `[0,1]` |
| `E_GRID` | 分块行列数不是正整数或不能整除输出分辨率 |
| `E_REGION` | 有效输出区域越界 |
| `E_SOURCE` | 原图缺失、PNG 无法读取、尺寸/摘要不匹配 |
| `E_DIGEST` | 持久化块摘要、像素摘要、版本或几何元数据无效 |
| `E_CONSIST` | 切片拼装与整幅坐标真值不一致 |
| `E_RESOURCE` | 线程数或内存预算非法、单块超过总预算 |
| `E_INJECTION` | 异常清单或注入基座不合法 |

可处理终止的约定退出码是 `20`；普通 CLI/配置致命错误为 `2`；批处理业务岗位失败会写入 `out/report.json`，使用 `--check` 时未被异常清单预期的失败会让退出码为 `1`。

## 验证结论

- Linux/Python 3.10 + NumPy 1.26：14 项测试全部通过。
- Windows Python 3.11.16 + NumPy 2.4.6 与 Python 3.13.14 + NumPy 2.5.2：均通过 14 项测试。
- 样本批处理：5 个正常岗位成功；`E_ANGLE`、`E_GRID`、`E_SHAPE`、`E_DIGEST` 四个注入全部匹配。
- 二次运行：5 个岗位分别复用 `12/16/9/36/48` 个块，新算块数均为 0。
- 强杀演练：延迟任务运行中 `kill -9` 前已原子提交 2 块；恢复后该岗位复用 2 块、补算 10 块，整幅一致性通过。
