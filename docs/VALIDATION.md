# 实际验证记录

验证时间：2026-11-25（Asia/Shanghai）。

## 运行环境

| 运行时 | numpy | sqlite3 | 结果 |
| --- | ---: | ---: | --- |
| Python 3.11.18（临时 venv：`/tmp/tb311venv`） | 2.4.6 | 3.53.1 | 9/9 测试通过，工作台通过 |
| Python 3.10.12（系统 `python3`，作为兼容回退） | 1.26.4 | 3.37.2 | 9/9 测试通过 |

任务建议 Python 3.11+；仓库代码使用 `from __future__ import annotations`，因此在带 numpy 的 Python 3.10 上也可运行。实际命令在 Linux 容器中执行；路径、临时文件和替换均使用 `pathlib`、`tempfile.mkstemp(dir=同目录)` 与 `os.replace`，并提供 CRLF 的 `.bat`，按 Windows 语义实现。本轮没有实际启动 Windows 进程。

## 自动化测试

```bash
/tmp/tb311venv/bin/python -m unittest discover -s tests -v
python3 -m unittest discover -s tests
```

两个运行时均为：

```text
Ran 9 tests
OK
```

覆盖：

- 样本 Schema 与 16 个高度场分块加载；
- 相机位置/目标重合返回 `E_SCHEMA` 且不创建输出目录；
- 编辑区域非整数或越界返回 `E_SCHEMA`；
- 内存上限为 1 块时按索引序调度且最大并发采样块数为 1；
- 首块异常保留首个错误和已成功块；
- 局部编辑后 4 块重渲、8 块复用；
- workers=4 与 workers=1 的灰度、深度、高程逐值相同；
- 中断退出码 130、标记写出、损坏元数据拒绝与重启恢复；
- 输出文件版本相同则跳过替换、版本变化才原子替换；
- NPZ 单字节篡改后 SHA-256 恢复校验失败。

## 一键工作台

```bash
/tmp/tb311venv/bin/python -m terrain_baker.workbench
```

产物：

- `artifacts/workbench/validation_summary.json`
- `artifacts/workbench/validation_report.md`
- `artifacts/workbench/index.html`

## 局部编辑验收

样本为 12 个屏幕分块（`256 × 192`，tile `64 × 64`）。局部“抬山脊 + 压河谷”后的实际结果：

```json
{
  "total_chunks": 12,
  "rerendered_chunks": 4,
  "reused_chunks": 8,
  "invalid_chunk_ids": [
    "chunk_r000_c000",
    "chunk_r001_c000",
    "chunk_r002_c000",
    "chunk_r002_c001"
  ]
}
```

串行等价：

```json
{
  "gray_exact_equal": true,
  "depth_exact_equal": true,
  "gray_max_abs_diff": 0,
  "depth_max_abs_diff": 0.0,
  "serial_workers": 1
}
```

与整幅 monolithic 全量重渲参照比较：

```json
{
  "gray_pixel_max_abs_diff": 0,
  "depth_world_max_abs_diff": 0.0,
  "height_world_max_abs_diff": 0.0,
  "normal_vector_max_abs_diff": 0.0,
  "boundary_band_normal_vector_max_diff_vs_full": 0.0,
  "boundary_band_normal_angle_max_diff_vs_full_degrees": 0.0,
  "heightfield_ownership_same_point_max_normal_deviation": 0.0,
  "heightfield_half_open_perturbation_max_normal_deviation": 5.99e-15,
  "boundary_normal_deviation_max": 0.0,
  "pixel_exact": true
}
```

`screen_boundary_adjacent_normal_angle_max_degrees` 是相邻屏幕像素在真实起伏地形上的自然法向夹角，不是分块拼装残差；报告同时给出与全量参照在边界带的向量差 `0.0`，可直接证明没有分块接缝。

## 光照、相机与 Schema

- 光照方向变化：12/12 块失效，`all_chunks_invalid=true`；
- 相机位置变化：12/12 块失效，`all_chunks_invalid=true`；
- 非法相机场景：CLI 退出码 `2`，JSON 错误码 `E_SCHEMA`，`output_directory_created=false`。

对应报告：

- `artifacts/workbench/lighting_change/report.lighting_change.json`
- `artifacts/workbench/camera_change/report.camera_change.json`

## 中断与恢复

工作台用 `--stop-after-completed 1` 触发分块边界停止，实际有两个已进入计算的块完成并提交：

```json
{
  "interrupt_exit_code": 130,
  "marker_written": true,
  "marker_recovery_record_written": true,
  "marker_completed_count": 2,
  "restart_exit_code": 0,
  "restart_counts": {
    "total_chunks": 12,
    "rerendered_chunks": 10,
    "reused_chunks": 2
  },
  "pixel_exact": true,
  "corrupt_metadata_rejected_and_recomputed": true,
  "stale_temp_files_ignored": true
}
```

强制终止场景通过残留 `.tmp` 与损坏 JSON 模拟：重启只承认 SHA-256 有效的已提交块，损坏块自动重算。

## 人工复核入口

- 灰度图：`artifacts/workbench/local_edit/out/terrain_gray.png`
- 深度预览：`artifacts/workbench/local_edit/out/terrain_depth.png`
- 全量参照：`artifacts/workbench/local_edit/out/full_reference_gray.png`
- 主报告：`artifacts/workbench/local_edit/report.local_edit.json`
- HTML 工作台：`artifacts/workbench/index.html`
