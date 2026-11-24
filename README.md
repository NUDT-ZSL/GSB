# Terrain Baker Workbench

本地、确定性的地形高度场离线烘焙引擎与诊断工作台。输入 JSON 场景和按固定种子生成的多块 `.npy` 高度场，输出：

- `terrain_gray.png`：8-bit Lambert 灰度着色图；
- `terrain_depth.npy` / `terrain_height.npy`：世界单位的光线距离深度与表面高程；
- `terrain_height_depth.npz`：深度、高程、命中掩码打包；
- `terrain_depth.png`：16-bit 深度可视化；
- `terrain_normals.npy`：全局法线图；
- `bake_report.json`：逐块失效理由、缓存版本、并发/内存、接缝偏差和全量参照像素差；
- `state/cache.sqlite3` 与 `state/chunks/`：SQLite 索引和 SHA-256 校验的分块缓存。

仅依赖 `numpy` 与标准库（含 `sqlite3`、`zlib`、`concurrent.futures`），不需要 Docker、GPU、Pillow 或外部网络。

## 1. 环境

推荐 Python 3.11+：

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

当前代码使用 `from __future__ import annotations`，也可在带 numpy >= 1.26 的 Python 3.10 上运行。已验证：

- Python 3.11.18 + numpy 2.4.6；
- Python 3.10.12 + numpy 1.26.4；
- SQLite 标准库可用。

## 2. 生成样本

Windows：

```powershell
python tools\generate_samples.py --output samples
```

跨平台等价命令：

```bash
python -m terrain_baker.cli generate-sample --output samples
```

生成内容：

- `samples/heightfields/tile_rXX_cYY.npy`：16 个半开区间分块，全局为 `128 × 128`；
- `samples/scene.initial.json`：相机、光照、分辨率、步进、并发和内存上限；
- `samples/edits/local_ridge_valley.json`：两个整数格索引声明的局部雕刻区域；
- `samples/apply_local_edit.py` 与 `samples/apply_local_edit.bat`：编辑脚本。

## 3. 一键诊断工作台

```powershell
run_workbench.bat
:: 或
python -m terrain_baker.workbench
```

工作台在 `artifacts/workbench/` 下自动完成：

1. 生成确定性样本；
2. 首次全量烘焙；
3. 复制样本并应用“抬山脊 + 压河谷”局部编辑；
4. 按声明区域反演失效块，只重渲相交块；
5. 用 `--workers 1` 全新串行烘焙并逐像素/逐深度比较；
6. 分别验证光照变化、相机变化导致全部 12 块失效；
7. 验证非法相机场景返回 `E_SCHEMA` 且不创建输出目录；
8. 验证分块边界信号退出码 `130`、中断标记、损坏状态拒绝和重启恢复；
9. 写出 `validation_summary.json`、`validation_report.md` 与可打开的 `index.html`。

最近一次结果：局部编辑后 `4` 块重渲、`8` 块复用；灰度最大像素差 `0`，深度最大差 `0.0`，并发与 workers=1 完全一致。

## 4. 手动验收

```powershell
# 1) 首次全量烘焙
python -m terrain_baker.cli bake `
  --scene samples\scene.initial.json `
  --output-dir artifacts\demo\out `
  --state-dir artifacts\demo\state `
  --mode initial

# 2) 雕刻高度场并生成 scene.local_edit.json
python -m terrain_baker.cli apply-edit --sample-dir samples

# 3) 按整数格编辑区域做增量烘焙
python -m terrain_baker.cli bake `
  --scene samples\scene.local_edit.json `
  --output-dir artifacts\demo\out `
  --state-dir artifacts\demo\state `
  --report artifacts\demo\report.local_edit.json `
  --mode terrain_edit `
  --edit samples\edits\local_ridge_valley.json
```

报告中查看：

- `counts.rerendered_chunks` / `counts.reused_chunks`；
- `chunks[*].reasons`；
- `invalidation_matrix`：局部地形、光照、相机三类逐块失效集合；
- `seam_validation.gray_pixel_max_abs_diff`；
- `seam_validation.depth_world_max_abs_diff`；
- `seam_validation.boundary_normal_deviation_max`；
- `seam_validation.pixel_exact`。

光照或相机对照可由工作台自动生成，也可复制场景 JSON 修改后执行：

```bash
python -m terrain_baker.cli bake --scene changed.json --output-dir out --state-dir state \
  --mode lighting --changed-key direction
python -m terrain_baker.cli bake --scene moved_camera.json --output-dir out --state-dir state \
  --mode camera --changed-key position
```

## 5. 测试

```powershell
run_tests.bat
:: 或
python -m unittest discover -s tests -v
```

测试覆盖 Schema、越界编辑、内存闸门的索引序、首错处理、局部失效、串行等价、归档、中断恢复、SHA-256 防篡改和原子输出版本判断。

## 6. 设计要点

### 全局采样与无接缝法线

所有渲染只读取缝合后的 `GlobalHeightField`。世界坐标先经 `origin` 与各轴 `world_scale` 换成连续全局格索引，再做双线性采样。法线使用 `gx ± radius`、`gz ± radius` 的全局高度中心差，并除以 `2 * radius * world_scale`，因此不会调用任何分块局部差分。

分块清单使用半开区间 `[row_start, row_end_exclusive)` / `[col_start, col_end_exclusive)`，Schema 会逐格检查无重叠、无缺口、完整覆盖 `global_rows × global_cols`。

### 分块依赖图与失效反演

每个分块记录：

- `pixel_bounds`：屏幕像素半开区间；
- `global_grid_coverage`：命中表面覆盖的全局格范围；
- `height_dependency_range`：光线步进、二分求精和法线采样实际触及的高度依赖区间；
- `dependency_digest`：高度切片、相机、光照、步进和输出布局的摘要；
- `dependency.nodes/edges`：高度区间、相机、光照、步进节点及 Lambert 传递关系。

失效规则：

- 地形编辑：声明的整数格区域与 `height_dependency_range` 半开区间相交才失效；
- 光照变化：全部失效；
- 相机变化：全部失效；
- 步进/分辨率变化：全部失效；
- 缓存缺失或 SHA-256 无效：失效重算。

### 持久化、版本与归档

全局版本摘要是所有高度场原始文件哈希、相机、光照、步进和分辨率的规范化 SHA-256。每块另有局部依赖摘要。

提交顺序：

1. 同目录 `.tmp` 写入 NPZ 并 `flush/fsync`；
2. `os.replace` 原子替换 `<chunk>.npz`；
3. 同目录 `.tmp` 写入 JSON 提交元数据；
4. `os.replace` 原子替换 `<chunk>.json`；
5. 事务更新 SQLite。

若旧 JSON 的 `global_version` 与新版不同，旧 NPZ/JSON 会先移动到 `state/chunks/archive/<version>_<dependency>/`，不会直接覆盖。恢复时只接受 JSON 可解析、状态为 `committed` 且 NPZ SHA-256 匹配的块。

### 中断与强制终止

`SIGINT`、`SIGTERM` 和 Windows 上可用的 `SIGBREAK` 只设置 `BoundarySignal`。调度器不会在单块渲染中途打断；当前块提交后，未开始块被取消。处理过的终止会原子写出 `state/INTERRUPTED.json`，CLI 返回约定退出码 `130`。

`kill -9`、断电或进程崩溃不会写标记；重启时临时文件和哈希不匹配的元数据不会被当作完成块，未提交块全部重算。成功恢复后旧标记会保存为 `INTERRUPTED.recovered.json`。

### 并发与内存

`ThreadPoolExecutor(max_workers=scene.scheduler.max_workers)` 负责有界并发。`OrderedMemoryGate` 按分块索引顺序授权采样缓冲；当 `used_bytes + estimated_chunk_buffer > memory_budget_bytes` 时，后续块等待，不会减小光线步长、法线半径或插值精度。

任一块抛出异常时设置取消事件、取消队列中其余 `Future`，保留首个异常，并在 `bake_report.json` / 错误详情中记录已成功块。

## 7. 目录

```text
terrain_baker/          引擎与工作台源码
tools/                  根目录入口脚本
samples/                首次生成的确定性样本
tests/                  unittest 测试
artifacts/workbench/    一键验证报告、JSON 摘要和 HTML 工作台
docs/VALIDATION.md      实际验证记录
```
