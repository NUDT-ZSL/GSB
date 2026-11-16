# 离线视频目标检测与候选匹配管线

本项目在本地完成多路 AVI 视频的滑窗检测、候选框聚合、跨帧 IoU 匹配、轨迹串联、确定性并行调度、帧预算截断、异常隔离和阶段快照恢复。运行过程不使用 Docker、不访问网络目标，只处理指定目录中的本地样本。

## 目录

```text
video_pipeline/              核心实现与 CLI
  video.py                   扫描、容器头校验、逐帧解码
  detection.py               积分图滑窗特征、融合评分、NMS
  tracking.py                按类别 IoU 匹配、候选池、轨迹状态
  budget.py                  优先级/已消耗帧数驱动的确定性预算分配
  snapshots.py               detection/matching 阶段原子快照
  pipeline.py                并行执行、恢复、异常隔离、结果报告
configs/                     正常、满预算、串行与非法配置
samples/                     4 路确定性合成 AVI 与逐帧真值
invalid_samples/             损坏容器头与截断 AVI
scripts/                     样本/异常样本辅助脚本
tests/                       20 个自动化验收用例
artifacts/                   已生成的最终报告、快照与验证摘要
```

## 环境与运行

本机实测环境：Python `3.11.0rc1`、`numpy==1.26.4`、`opencv-python==4.10.0.84`、`pytest==8.3.5`。Python 要求 `>=3.10`。

```bash
python3 -m pip install -r requirements.txt
python3 scripts/generate_samples.py \
  --output samples --ground-truth samples/ground_truth.json
python3 scripts/generate_invalid_samples.py --output invalid_samples
```

生成样本为 MJPG AVI，包含 `vehicle`（红车）和 `pedestrian`（蓝人）几何目标、确定性的背景噪声与灰色干扰几何体；`samples/ground_truth.json` 保存每个目标逐帧真值框。

完整预算运行：

```bash
python3 -m video_pipeline \
  --input samples --model configs/model.json \
  --matching configs/matching_full.json \
  --output artifacts/report_full.json \
  --state-dir artifacts/state_full --reset
```

串行复现与预算截断：

```bash
python3 -m video_pipeline --input samples --model configs/model.json \
  --matching configs/matching_serial.json \
  --output artifacts/report_serial.json --state-dir artifacts/state_serial --reset

python3 -m video_pipeline --input samples --model configs/model.json \
  --matching configs/matching.json \
  --output artifacts/report_budget.json --state-dir artifacts/state_budget --reset
```

恢复时去掉 `--reset` 并指向原快照目录即可。收到 `SIGINT` 或 `SIGTERM` 时，管线完成当前帧、写快照与报告，然后以退出码 `0` 结束；`SIGKILL` 只保证已原子替换的快照存在。

## 配置协议

`configs/model.json`：

- `windows[]`：`id`、`class_name`、`width`、`height`、`step`，支持多尺度。
- `target_colors`：类别到 BGR 目标颜色；`color_tolerance` 为逐通道最大绝对差。
- `color_ratio_threshold`：窗口内目标颜色像素比例门限。
- `edge_pixel_threshold`：Sobel 梯度幅值二值化门限；`edge_density_threshold` 为窗口边缘像素比例门限。
- `fusion_weights.color/edge`：颜色均值相似度与边缘密度的融合权重。
- 颜色均值为窗口 BGR 均值与目标色逐通道接近度的平均；评分为
  `(color_weight * color_mean_similarity + edge_weight * edge_density) / (color_weight + edge_weight)`。
- `detection_threshold`：候选得分满足 `score >= threshold - 1e-9` 时输出。
- `nms_iou_threshold`：同类候选按得分降序 NMS，仅当 `IoU > threshold + 1e-9` 时抑制；相等边界保留。

`configs/matching.json`：

- `max_workers`：工作线程上限；`total_frame_budget`：全局总帧预算。
- `iou_match_threshold`：连续帧候选与轨迹在 `IoU >= threshold - 1e-9` 时匹配。
- `candidate_pool_capacity`：`vehicle`、`pedestrian` 各自轨迹池容量。
- `routes[]`：`name`、`priority`、`frame_budget`；未列出的视频使用 `default_route`。
- `snapshot_every_frames`：每处理多少帧写一次完整阶段快照，最终仍强制写盘。

配置哈希覆盖检测参数、匹配阈值、候选池、路线优先级/预算等语义字段；不包括 `max_workers` 和快照频率，因此仅切换并发数不会导致不可恢复。快照若来自不同语义配置，会输出 `E_CONFIG_MISMATCH` 并拒绝恢复，随后从干净状态运行。

## 调度与确定性

预算分配在入场前确定，每次选择待分配路线时使用稳定键
`(-priority, allocated_frames, route_name)`，即高优先级优先、同优先级已消耗少者优先、路线名最终打破平局。每条路线可处理上限为
`min(source_frame_count, route_frame_budget)`；分配不足时输出 `E_BUDGET_EXHAUSTED`，`truncated_frame` 为从 0 开始的下一未处理帧。线程池只接收已分配帧范围，不会越界解码或无界排队。

检测与匹配均按路线提交线程池，线程池通过上下文管理器关闭并等待工作线程回收。检测和匹配分别使用两阶段线程池；`peak_workers` 为实际同时活跃的路线任务峰值。

确定性规则：

- 候选稳定键为 `route|10位帧号|6位窗口索引`。
- 每帧候选按 `(-score, class_name, stable_key)` 排序；NMS 按类别分别执行。
- 匹配对按 `(-IoU, local_track_id, candidate_stable_key)` 贪心选择。
- 新轨迹本地 ID 由路线内候选稳定顺序分配；最终全局 `track_id` 按
  `(created_frame, route_scan_order, local_track_id)` 从 1 重新编号。
- 轨迹内帧序列按帧号排序；路线按相对路径排序。
- `result_hash` 只覆盖轨迹、路线语义状态、失败清单和预算计数，不覆盖耗时、线程峰值、恢复标记等运行时元数据。并行与串行运行应得到相同 `result_hash`。

## 快照与异常

状态目录包含：

- `detection_snapshot.json`：阶段名、配置哈希、每路线连续帧游标、逐帧候选和失败。
- `matching_snapshot.json`：阶段名、配置哈希、每路线匹配游标、轨迹摘要和失败。

写盘使用同目录临时文件、`fsync`、`os.replace` 和目录 `fsync`。恢复时只接受连续游标及不超过游标的轨迹帧；不一致的匹配状态会重置并记录 `E_SNAPSHOT_INVALID`。所有最终报告也以同样方式原子写出。

失败清单字段固定为：`route`、`frame_index`、`stage`、`code`、`exception_type`、`message`。主要业务码：

| 业务码 | 含义 |
| --- | --- |
| `E_DECODE_FAIL` | 容器无法打开或解码中途失败，记录失败帧号 |
| `E_BUDGET_EXHAUSTED` | 路线/全局帧预算不足，`frame_index` 即截断帧号 |
| `E_CONFIG_INVALID` | 全局配置非法（退出码 2），或单路线配置项被跳过 |
| `E_CONFIG_MISMATCH` | 快照语义配置哈希不一致，拒绝恢复 |
| `E_DETECTION_FAIL` | 单帧检测异常，已隔离并继续 |
| `E_MATCHING_FAIL` | 单帧匹配异常，已回滚该帧轨迹变更并继续 |
| `E_SNAPSHOT_INVALID` | 快照损坏或游标/轨迹越界 |
| `E_SNAPSHOT_WRITE` | 阶段快照写入失败 |
| `E_NO_VIDEOS` | 输入目录没有支持的视频 |

退出码：完整运行、预算截断、可隔离的单路/单帧异常、优雅信号退出均为 `0`；配置或输入目录非法为 `2`；未预期内部错误为 `1`。强制终止不承诺生成退出报告。

## 结果协议

最终 JSON 的主要字段：

```json
{
  "status": "completed",
  "config_hash": "...",
  "result_hash": "...",
  "tracks": [
    {
      "track_id": 1,
      "class_name": "vehicle",
      "route": "cam_front.avi",
      "created_frame": 0,
      "first_frame": 0,
      "last_frame": 17,
      "center": [204.0, 202.0],
      "velocity": [8.0, 0.0],
      "frames": [
        {
          "frame_index": 0,
          "timestamp_ms": 0.0,
          "bbox": [24, 184, 80, 220],
          "score": 0.502,
          "stable_key": "cam_front.avi|0000000000|000000"
        }
      ]
    }
  ],
  "routes": [
    {
      "route": "cam_front.avi",
      "priority": 30,
      "frame_budget": 18,
      "source_frame_count": 18,
      "allocated_frames": 18,
      "processed_frames": 18,
      "matched_frames": 18,
      "truncated_frame": null,
      "status": "completed"
    }
  ],
  "failures": [],
  "budget": {
    "max_workers": 4,
    "total_frame_budget": 200,
    "allocated_frames": 62,
    "frames_processed": 62,
    "remaining_frame_budget": 138,
    "peak_workers": 4,
    "elapsed_seconds": 3.6
  },
  "recovery": {
    "resume_enabled": false,
    "detection_snapshot_loaded": false,
    "matching_snapshot_loaded": false
  }
}
```

框坐标 `[x1, y1, x2, y2]` 为整数、右/下边界开区间，已裁剪到帧边界。时间戳为 `frame_index / fps * 1000` 毫秒。

## 实测验收

验证日期：2026-11-17。最终自动测试命令与结果：

```text
python3 -m pytest -q
20 passed
```

已生成的证据位于 `artifacts/`：

| 场景 | 实测结果 |
| --- | --- |
| 满预算并行 | 62/62 帧、6 条轨迹、`result_hash=9bdf9d80...`、`peak_workers=4` |
| 满预算串行 | 与并行轨迹/路线/失败/哈希逐字段一致、`peak_workers=1`、同一 `result_hash=9bdf9d80...` |
| 全局预算 36 | 36/36 帧；分配 14/12/8/2；3 条 `E_BUDGET_EXHAUSTED`，截断帧为 14、12、2 |
| 快照恢复 | 完整快照重启后 `result_hash` 仍为 `9bdf9d80...`，轨迹逐字段一致 |
| 配置失配 | 输出 1 条 `E_CONFIG_MISMATCH`，恢复标记均为 false，随后全新运行 |
| 损坏样本隔离 | `bad_header.avi` 在帧 0、`truncated.avi` 在帧 2 记录 `E_DECODE_FAIL`，批次继续 |
| 路线配置隔离 | 缺失 `frame_budget` 的路线记录 `E_CONFIG_INVALID`，其余 3 路照常完成 |
| `SIGTERM` | 退出码 0、`status=graceful_stop`、已提交 9 帧并生成预算报告 |
| 非法配置 | 退出码 2，原子报告含 `E_CONFIG_INVALID` |

`artifacts/validation_summary.json` 汇总了版本、哈希、失败码与统计；各 `report_*.json` 为对应运行的原始报告。注意 `elapsed_seconds`、`peak_workers`、恢复游标属于运行时元数据，不参与跨并发/恢复的 `result_hash` 等价判断。
