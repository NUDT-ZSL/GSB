# 输入输出格式与错误码

## 1. 点云二进制

小端序。24 字节头：

| 偏移 | 类型 | 含义 |
|---:|---|---|
| 0 | `8s` | 魔数 `PCWBPTS1` |
| 8 | `uint32` | 版本，当前 `1` |
| 12 | `uint32` | 点数 `N` |
| 16 | `uint32` | 每条记录字节数，固定 `32` |
| 20 | `uint32` | 标志，固定 `0` |

随后是 `N` 条 `float32[8]`：

```text
x, y, z, gray, nx, ny, nz, radius
```

校验要求所有值有限，`0 <= gray <= 1`，法线长度大于 `1e-12`，`radius >= 0`，文件长度严格等于 `24 + N*32`。坐标先按任务的世界尺度与原点偏移变换；半径乘以世界尺度。

## 2. 任务 JSON

```json
{
  "schema_version": 1,
  "task_id": "sparse_scan_art_review",
  "cache_key": "sample_scene_seed_20261125",
  "pointcloud": {
    "path": "points_base.bin",
    "world_scale": 0.01,
    "origin_offset": [0.02, -0.01, 0.03]
  },
  "render": {
    "width": 96,
    "height": 64,
    "supersample": 2,
    "blocks": {"width": 16, "height": 16},
    "max_workers": 4,
    "memory_budget_bytes": 65536,
    "round_budget_units": 150.0
  },
  "cameras": [
    {
      "id": "cam_front",
      "position": [0.15, 0.25, 3.1],
      "target": [0.0, 0.0, 0.0],
      "up": [0.0, 1.0, 0.0],
      "vertical_fov_deg": 50.0,
      "aspect_ratio": 1.5
    }
  ],
  "edit_manifest": "edits.json"
}
```

约束：

- `width/height/block.width/block.height/max_workers/memory_budget_bytes` 为正整数。
- `supersample`、`aspect_ratio`、`round_budget_units` 为正有限数。
- `vertical_fov_deg` 严格位于 `(0, 180)`。
- 相机位置与目标不能重合；上方向不能与视线平行。
- 块网格必须无重叠地覆盖每个输出像素一次。
- 内存预算必须至少容纳一个最大块的采样缓冲；否则报 `E_BUDGET`，不自动降低超采样。
- 所有相对路径相对任务 JSON 所在目录解析。

## 3. 编辑清单

```json
{
  "schema_version": 1,
  "base_pointcloud": "points_base.bin",
  "edited_pointcloud": "points_edited.bin",
  "base_dataset_digest": "...",
  "edited_dataset_digest": "...",
  "regions": [
    {
      "id": "edit_00",
      "start": 900,
      "count": 32,
      "reason": "rescan patch A",
      "old_digest": "...",
      "new_digest": "..."
    }
  ]
}
```

区域按点索引连续定义，不允许重叠或越界。`old_digest/new_digest` 是区域原始 `float32` 记录的 SHA-256。任务点云必须与 `edited_dataset_digest` 一致，基线文件必须与 `base_dataset_digest` 一致。

## 4. 块缓存 NPZ

每个 `state/<cache_key>/<camera_id>/blocks/bNNNN.npz` 包含：

| 数组 | dtype | 含义 |
|---|---|---|
| `gray` | `uint8` | 块输出灰度 |
| `depth` | `float32` | 世界单位深度，背景 `inf` |
| `dependencies` | `int64` | 可能覆盖块内样本的点索引，严格递增 |
| `meta` | `uint8` | UTF-8 规范 JSON 元数据 |

元数据关键字段：`block_index`、`rect`、`version_id`、`dataset_digest`、`camera_digest`、`render_digest`、`point_count`、`dependency_count`、`dependency_digest`、`declared_pixel_count`、`content_digest`、`provenance`。

相机提交文件 `committed.json` 保存已完成的版本与整图摘要。中断标记位于 `state/<cache_key>/INTERRUPTED.json`，包含退出码 130、已完成块和未准入块。

## 5. 发布产物

每个相机目录：

```text
grayscale.png       8 位灰度投影
depth.pfm           32 位小端世界深度 PFM，背景 inf
depth_preview.png   仅用于观察的归一化深度预览
blocks.json         分块区域、状态、失效理由、依赖点集与摘要
```

输出根目录：

```text
render_report.json  任务、相机、失效统计、调度证据、逐像素验证
workbench.html      单文件离线交互诊断
```

`render_report.json` 中的 `rendered_block_count` 是本轮初始补渲块数，`invalidated_block_count` 包含初始失效与最终摘要修复块，`reused_block_count` 只含复用/版本迁移块。`digest_repairs` 中每项带 `business_code: "E_DIGEST"`、原始摘要失败原因和 `repaired` 结果；只有定向修复后仍失败才会把 `E_DIGEST` 作为进程错误返回。

## 6. 错误码

| 业务码 | 含义 |
|---|---|
| `E_ARGUMENT` | CLI 参数错误 |
| `E_JSON` | JSON 无法读取、编码无效或语法错误 |
| `E_SCHEMA` | 任务/清单结构或字段类型错误 |
| `E_POINTCLOUD_IO` | 点云文件不可读 |
| `E_POINTCLOUD_FORMAT` | 魔数、版本、头、记录大小或文件长度错误 |
| `E_POINT_DATA` | 坐标/灰度/法线/半径非法 |
| `E_WORLD` | 世界尺度或原点偏移非法 |
| `E_CAMERA` | 相机位置/目标/上方向非法或上方向与视线平行 |
| `E_FOV` | 垂直视场角不在 `(0,180)` |
| `E_ASPECT` | 宽高比非有限正数 |
| `E_SUPERSAMPLE` | 超采样倍率非有限正数 |
| `E_BLOCKS` | 分辨率或分块规格非法/覆盖错误 |
| `E_BUDGET` | 并发或内存预算无法满足单块采样 |
| `E_EDIT` | 编辑清单、区域摘要或谱系校验失败 |
| `E_STATE` | 状态目录不可写或状态操作失败 |
| `E_RENDER` | 块渲染 worker 异常 |
| `E_SCHEDULER` | 调度器输入非法 |
| `E_DIGEST` | 定向补渲后摘要/像素数/块内容仍无效 |
| `E_PIXEL_MISMATCH` | `--verify-full` 与串行整幅重算不一致 |
| `E_OUTPUT` | 最终产物写入失败 |
| `E_VIEW` | 持久化诊断产物缺失或 Tk 显示不可用 |
| `E_INTERNAL` | 未预期内部异常 |
| `E_INTERRUPTED` | 收到可处理停止信号，JSON 标记中断 |

退出码：成功 `0`，内部错误 `1`，输入/业务错误 `2`，未修复摘要错误 `3`，中断 `130`。

## 7. 错误 JSON

```json
{
  "ok": false,
  "error": {
    "code": "E_POINT_DATA",
    "message": "point 7 has invalid coordinates",
    "details": {"point_index": 7, "field": "coordinates"}
  },
  "exit_code": 2
}
```

