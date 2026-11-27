# 潜水训练视频教学分段复核报告

## 运行摘要

- 随机种子：`20240518`
- 帧数：`480`
- 原始/稳定边界：`10` / `4`
- 稳定镜头：`5`
- 教学段落：`4`
- 事件：`8`
- 浑浊区间：`2`
- 内容哈希：`6074ec03b818234a20220bdceaa0bd35ad9d16962f473a845120ac41d22341dc`

## 镜头切分与修正

| 边界帧号 | 时间(s) | 处理 | 原因 |
| ---: | ---: | --- | --- |
| 102 | 12.625 | retained | meets_minimum_shot_duration |
| 181 | 22.500 | removed | turbidity_change_filtered |
| 194 | 24.125 | removed | single_frame_segment_removed |
| 195 | 24.250 | removed | single_frame_segment_removed |
| 207 | 25.750 | retained | meets_minimum_shot_duration |
| 249 | 31.000 | removed | turbidity_change_filtered |
| 295 | 36.750 | retained | meets_minimum_shot_duration |
| 357 | 44.500 | removed | turbidity_change_filtered |
| 385 | 48.000 | retained | meets_minimum_shot_duration |
| 429 | 53.500 | removed | turbidity_change_filtered |

## 分段与能见度

| 段落 | 帧范围 | 镜头 | 拟合分数 | 等级跨度 | 事件类型 |
| --- | --- | --- | ---: | --- | --- |
| P01 | 1-101 | S01 | 70.33 | 0-0 | - |
| P02 | 102-294 | S02, S03 | 48.17 | 1-2 | shot_cut, turbidity_influx, turbidity_recovery |
| P03 | 295-384 | S04 | 54.99 | 1-1 | shot_cut, turbidity_influx |
| P04 | 385-480 | S05 | 44.06 | 2-2 | shot_cut, turbidity_recovery |

## 事件表

| 事件 | 帧号 | 时间(s) | 类型 | 裁定依据 | 镜头 |
| --- | ---: | ---: | --- | --- | --- |
| E01 | 102 | 12.625 | shot_cut | only_shot_boundary_detected | S02 |
| E02 | 173 | 21.500 | turbidity_influx | only_visibility_change_detected | S02 |
| E03 | 207 | 25.750 | shot_cut | only_shot_boundary_detected | S03 |
| E04 | 259 | 32.250 | turbidity_recovery | only_visibility_change_detected | S03 |
| E05 | 295 | 36.750 | shot_cut | only_shot_boundary_detected | S04 |
| E06 | 349 | 43.500 | turbidity_influx | only_visibility_change_detected | S04 |
| E07 | 385 | 48.000 | shot_cut | only_shot_boundary_detected | S05 |
| E08 | 439 | 54.750 | turbidity_recovery | only_visibility_change_detected | S05 |

## 一致性

- `shot_ids_match_scores`: PASS
- `shot_ids_match_ranges`: PASS
- `segments_cover_every_shot_once`: PASS
- `shot_frames_are_contiguous`: PASS
- `segment_frames_are_contiguous`: PASS
- `event_frames_unique`: PASS
