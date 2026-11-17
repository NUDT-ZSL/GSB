# 技术说明

## 1. 模块划分

| 模块 | 职责 |
| --- | --- |
| `recon_workbench/samples.py` | 固定种子合成场景、相机、PNG 与注入文件落盘 |
| `recon_workbench/io_views.py` | 视图扫描、PNG/相机校验、分辨率与序号唯一性、清单哈希 |
| `recon_workbench/features.py` | 色度纹理响应、局部极大值、稳定排序与 75 维描述子 |
| `recon_workbench/geometry.py` | 双向最近邻匹配、比值检验、相机内点判定、DLT 三角化 |
| `recon_workbench/depth.py` | 点投影、深度量化、4 邻域确定性平滑 |
| `recon_workbench/pointcloud.py` | 体素分组、稳定代表点、全局点云排序 |
| `recon_workbench/caching.py` | 内容寻址缓存键、带 payload 哈希的原子缓存 |
| `recon_workbench/snapshots.py` | 版本化阶段快照、最新快照扫描、恢复校验 |
| `recon_workbench/tasks.py` | 有界线程池、停止投递、异常隔离、稳定结果重排 |
| `recon_workbench/pipeline.py` | 阶段编排、依赖失效、信号中断、报告与恢复 |
| `recon_workbench/cli.py` | 生成、注入物化、运行、报告比较和进程退出码 |

## 2. 阶段与顺序

### 2.1 视图清单

扫描 `views/view_<下划线序号>.png` 与 `cameras/view_<序号>.json`。每个视图依次检查：

1. 图像和相机文件成对存在；
2. 图像、相机文件中的视图序号都唯一，且与文件名一致；
3. PNG 8 字节容器头正确；
4. Pillow 可完整解码，得到 RGB 与灰度数组；
5. 全样本采用出现次数最多的分辨率；不同分辨率视图失败；
6. 相机 JSON、内参、旋转正交性、平移、尺寸合法。

任何失败都写入 `E_VIEW_INVALID`，视图不进入后续阶段，其他视图继续。

### 2.2 特征提取

检测器 `chromatic_blob_v1` 是无随机数的确定性色度局部极大值检测器：

1. `colorfulness = (max(R,G,B)-min(R,G,B))/255`；
2. 固定 5x5 高斯核卷积；
3. 在配置半径内做局部极大值比较；
4. 按响应、y、x 稳定排序，执行最小距离抑制和数量上限；
5. 每个点采样 5x5 个 3x3 RGB 块均值，形成 75 维描述向量；
6. 最终特征按 `(y, x)` 排序并分配 `F-V<视图>-<序号>`。

样本中的纹理标记使用唯一色相和 5x5 亮度码，保证相邻视图有大量稳定、可区分的对应特征。

### 2.3 匹配、内点与三角化

匹配对包括：

- 有效特征视图排序后的相邻对；
- 配置 `view_matching.loop_pairs` 指定的回环对。

匹配算法：

1. A 到 B、B 到 A 分别取最近描述子；
2. 只保留互为最近邻的匹配；
3. 应用最大描述距离和最近邻/次近邻比值；
4. 匹配按特征索引稳定排序。

每个描述子匹配再通过相机几何判定内点：

- 四点 DLT 三角化；
- 两个相机中的深度都为正且在深度上下界内；
- 双视图重投影误差不超过阈值；
- 相机中心到三维点两条射线的夹角不小于最小视差角。

匹配记录内点布尔标记；内点数小于 `min_inliers` 时该对输出 `E_MATCH_INSUFFICIENT`，不向全局点云贡献点。匹配函数内部立即把视图对规范化为升序，因此反向调用得到完全相同的匹配、坐标和内点标记。

### 2.4 深度层

视图的深度输入是该视图参与的成功匹配对点。点的世界坐标投影到量化网格，每个单元保留最近深度，深度相同则按 `point_id` 稳定决胜。

量化公式：

```text
q = 1 + round((depth-min_depth)/(max_depth-min_depth)*(levels-2))
```

背景为 0，前景范围为 `[1, levels-1]`。平滑对每个非零单元使用有效 4 邻域均值：

```text
next = (1-weight)*current + weight*mean(valid_neighbors)
```

迭代次数和权重来自配置，最后再稳定取整。完整深度层按视图序号写入 `depth_layers.json`。

### 2.5 体素合并

所有成功匹配对的三角化点进入体素分组：

```text
voxel = (floor(x/voxel_size), floor(y/voxel_size), floor(z/voxel_size))
```

同体素内按 `(x, y, z, point_id)` 字典序排序，第一个点为代表点，并记录 `merged_point_count`。全局点云按：

```text
(最小来源视图, 最大来源视图, voxel_x, voxel_y, voxel_z, representative_point_id)
```

排序，因而输出顺序与输入视图顺序绑定，而不依赖线程完成顺序。规则原文同时写入 `cloud.json`。

## 3. 内容指纹与缓存依赖

缓存文件包装中包含 `cache_schema_version`、类别、键、payload SHA-256 和 payload。读取时键与 payload 哈希都要校验。依赖关系如下：

| 产物 | 缓存键上游 |
| --- | --- |
| 视图特征 | 图像字节哈希 + 标准化相机哈希 + 特征参数 + 种子 + 视图序号 + 注入配置哈希 + 实现版本 |
| 匹配/三角化 | 两个特征键 + 两个相机哈希 + 匹配参数 + 三角化参数 + 注入配置哈希 |
| 深度层 | 视图相机哈希 + 该视图所有贡献匹配键 + 深度参数 + 三角化深度参数 + 图像尺寸 |
| 全局点云 | 所有匹配键 + 所有深度层键 + 体素参数 + 注入配置哈希 |

未变化的键会直接读取已有 payload，不调用计算函数。报告中的 `run.cache.*.reused_keys` 与 `recomputed_keys` 可用于检查实际执行范围。

实测单视图图像或相机变化时：

- 仅该视图特征重算；
- 仅包含该视图的 3 个匹配对重算；
- 仅由这些匹配对提供输入的 4 个深度层重算；
- 全局点云重算。

仅调整深度平滑参数时，8 个特征和 9 个匹配全部复用，8 个深度层和全局点云重算。

## 4. 快照与恢复

快照按递增序号写为 `snapshots/snapshot-XXXXXXXX.json`，通过同目录临时文件、`fsync` 和 `os.replace` 原子提交。扫描目录取最大完整序号作为最新快照。结构包含：

```json
{
  "snapshot_schema": "recon-workbench-snapshot",
  "snapshot_schema_version": 1,
  "snapshot_version": 1,
  "version": 1,
  "sequence": 16,
  "stage": "matching",
  "stage_complete": false,
  "config_hash": "...",
  "camera_hash": "...",
  "input_hash": "...",
  "injection_hash": "...",
  "completed_views": [1, 2, 3, 4, 5, 6],
  "progress": {
    "features": {"completed_keys": ["..."], "completed_views": [1, 2]},
    "matching": {"completed_keys": ["..."], "completed_views": [1, 2]}
  },
  "partial_pointcloud_summary": {
    "reconstructed_point_count": 140,
    "source_pair_count": 1,
    "digest": "...",
    "finalized": false
  }
}
```

恢复流程：

1. 读取最大序号的完整快照；
2. 校验快照 schema、`snapshot_version` 和全量 `config_hash`；
3. 任一不一致输出 `E_CONFIG_MISMATCH`，退出码 3，不启动计算；
4. 一致时根据当前内容键检查已有缓存；
5. 已完成且键相同的特征、匹配、深度和点云直接复用，其余任务重新执行；
6. 每完成一个配置间隔的视图重新原子写进度快照。

相机或输入内容变化不直接拒绝恢复，而是通过内容键精确失效受影响产物。配置参数变化使全量配置哈希改变，按规定拒绝续跑；使用新 `--state` 或显式 `--fresh`，同时共享 `--cache` 可执行增量重算。

## 5. 并发确定性与中断

`execute_tasks` 先按稳定 `sort_key` 排序。缓存读取在协调线程完成；未命中任务进入有界 `ThreadPoolExecutor`，最多同时提交 `executor.max_workers` 个。完成结果放入映射，最终再按相同排序键输出。

收到停止标志后：

1. 不再提交排队任务；
2. 等待已经提交的在途任务；
3. 每个任务内部以原子替换提交自己的缓存产物；
4. 协调线程写当前阶段的部分进度快照；
5. 生成 `status=interrupted` 报告并以 0 退出。

`SIGKILL`/`TerminateProcess` 不执行清理，但不会破坏已原子替换的文件。重启时未完成任务的临时文件不符合缓存文件名，也不会被快照扫描器识别。

## 6. 报告中的确定性边界

`report.json.result` 是确定性结果区，包含：

- `config_hash`（语义配置哈希）；
- 相机/输入哈希；
- 逐视图统计；
- 匹配对与深度层摘要；
- 重建点数和体素数；
- 点云与深度层摘要哈希；
- 失败清单和业务码；
- 注入命中；
- `snapshot_version`；
- 自包含 `result_digest`。

`report.json.run` 是运行诊断区，包含是否恢复、恢复点、缓存复用键、快照序号等，允许在中断恢复运行间不同。验收中要求 `result` 逐字段相同、几何产物逐字节相同。

全量配置哈希包含并发数和快照间隔，用于快照拒绝。语义配置哈希排除这两项执行控制，因此串行和并发运行可共享完全一致的结果。

## 7. 故障处理

| 注入类型 | 实际行为 | 业务码 |
| --- | --- | --- |
| `corrupt_png` | 物化时写损坏字节；运行时识别为损坏 PNG | `E_VIEW_INVALID` |
| `missing_camera` | 物化时删除相机文件；运行时按缺失处理 | `E_VIEW_INVALID` |
| `low_parallax` | 匹配对置为视差越界，记录注入原因 | `E_MATCH_INSUFFICIENT` |
| `nonfinite_descriptor` | 在首特征描述子写入 `NaN` 并检测 | `E_NONFINITE_FEATURE` |
| `stage_exception` | 指定视图/阶段抛任务异常，其他任务继续 | `E_STAGE_EXCEPTION` |
| `zero_voxel_size` | 修改体素尺寸后配置校验失败 | `E_CONFIG_INVALID` |
| `invalid_budget` | 把线程资源上限改为 0 后配置校验失败 | `E_CONFIG_INVALID` |

注入项 ID 从失败记录汇总到报告；未命中的选中注入项不会伪造为命中。

## 8. 需求追踪

| 业务要求 | 实现位置 | 验证 |
| --- | --- | --- |
| 视图校验、灰度/彩色解码、失败继续 | `io_views.py` | 测试 10、注入验收 |
| 特征阈值、坐标、描述子 | `features.py` | `features.json`、逐视图统计 |
| 相邻/回环匹配、内点、三角化、反向一致 | `geometry.py` | 测试 3 |
| 深度量化和平滑 | `depth.py` | `depth_layers.json` |
| 体素合并、代表规则、顺序绑定 | `pointcloud.py` | `cloud.json`、串行/并发字节比较 |
| 内容键与精确失效 | `caching.py`、`pipeline.py` | 测试 4/5、缓存计数 |
| 阶段快照、原子提交、恢复一致性 | `snapshots.py`、`pipeline.py` | 测试 1/8/9、验收恢复 |
| 线程池与串行逐字段一致 | `tasks.py` | 测试 2、四产物字节比较 |
| 注入业务码、编号与继续执行 | `injection.py`、`pipeline.py` | 测试 6、验收 4 个注入场景 |
| 信号退出、强杀恢复、完整报告 | `cli.py`、`pipeline.py` | 测试 8/9、验收信号场景 |
