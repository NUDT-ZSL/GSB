# 潜水训练视频教学分段复核报告

## 运行摘要

- 随机种子：`20240517`
- 帧数：`480`
- 原始/稳定边界：`10` / `4`
- 稳定镜头：`5`
- 教学段落：`3`
- 事件：`6`
- 浑浊区间：`1`
- 内容哈希：`c3304bea4c269faca1d9c572416903aab1ea7002a6e8888f7dee70041ace4945`

## 镜头切分与修正

| 边界帧号 | 时间(s) | 处理 | 原因 |
| ---: | ---: | --- | --- |
| 91 | 11.250 | retained | meets_minimum_shot_duration |
| 179 | 22.250 | retained | meets_minimum_shot_duration |
| 200 | 24.875 | removed | single_frame_segment_removed |
| 201 | 25.000 | removed | single_frame_segment_removed |
| 267 | 33.250 | removed | turbidity_change_filtered |
| 273 | 34.000 | retained | meets_minimum_shot_duration |
| 352 | 43.875 | removed | turbidity_change_filtered |
| 357 | 44.500 | removed | turbidity_change_filtered |
| 360 | 44.875 | removed | turbidity_change_filtered |
| 379 | 47.250 | retained | meets_minimum_shot_duration |

## 分段与能见度

| 段落 | 帧范围 | 镜头 | 拟合分数 | 等级跨度 | 事件类型 |
| --- | --- | --- | ---: | --- | --- |
| P01 | 1-272 | S01, S02, S03 | 66.91 | 0-1 | shot_cut, turbidity_influx |
| P02 | 273-378 | S04 | 33.94 | 2-2 | shot_cut, turbidity_recovery |
| P03 | 379-480 | S05 | 68.34 | 0-0 | shot_cut |

## 事件表

| 事件 | 帧号 | 时间(s) | 类型 | 裁定依据 | 镜头 |
| --- | ---: | ---: | --- | --- | --- |
| E01 | 91 | 11.250 | shot_cut | only_shot_boundary_detected | S02 |
| E02 | 179 | 22.250 | shot_cut | only_shot_boundary_detected | S03 |
| E03 | 258 | 32.125 | turbidity_influx | only_visibility_change_detected | S03 |
| E04 | 273 | 34.000 | shot_cut | only_shot_boundary_detected | S04 |
| E05 | 362 | 45.125 | turbidity_recovery | only_visibility_change_detected | S04 |
| E06 | 379 | 47.250 | shot_cut | only_shot_boundary_detected | S05 |

## 一致性

- `shot_ids_match_scores`: PASS
- `shot_ids_match_ranges`: PASS
- `segments_cover_every_shot_once`: PASS
- `shot_frames_are_contiguous`: PASS
- `segment_frames_are_contiguous`: PASS
- `event_frames_unique`: PASS
