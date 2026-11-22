# Validation Record

验证日期：2026-11-23（本地时区 Asia/Shanghai）

## 环境

- Python 3.10.12
- NumPy 1.26.4
- PyQt5 可导入
- 无网络、无真实数据集、无容器

## 自动测试

命令：

```bash
python3 -m compileall -q quantdiag tests main.py
python3 -m unittest discover -s tests -v
```

结果：

- `compileall`：通过
- 单元/集成/Qt 冒烟测试：`Ran 25 tests ... OK`

覆盖点包括三格式解析、字段级损坏、迁移补全/丢弃、冷热缓存、三类定向失效、追溯重放、过期结果、断链、跨格式等价/漂移、误差谱、快照回退/损坏/并发占用、固定样本可重复生成和 Qt offscreen 视图。

## 无窗口验收

命令：

```bash
python3 -m quantdiag acceptance --root workspace_data
```

结果：7 项检查全部 PASS。

```text
format_loading_and_dispatch
migration_fill_and_drop_provenance
byte_reproducible_and_content_addressed_cache
directed_dependency_invalidation
lineage_replay_stale_and_broken_detection
cross_format_semantics_and_error_spectrum
snapshot_rollback_corruption_busy_and_whole_replace
```

关键确定性证据：

- 分类分数校验和：`598014a5e000beb94cc5249454950d12053419f8f3cdd669903b654bbd95e409`
- 无缓存重算与热缓存复用的结果校验和一致：`0518d05169ce7c509bfede4a40269b7c872596d57cb4206620e565f98e0a3e3e`
- 热缓存复用层：`conv1, conv2, fc`
- conv1 键：`lk-fbea7b8e828365ef9941eac901f6937e0c169adc6187a462d12bdef8749ff438`
- conv2 键：`lk-998a117ec68eae360b5383cedfda82d26abe8829a770e143ed416721470abb8e`
- fc 键：`lk-4d15411367d44b30bd285ecceecb2af57b878cd61e933da5c79d4cf34f8a3f4e`

三类定向失效均从 `conv2` 开始失效，复用 `conv1`，仅重算 `conv2, fc`：

| 场景 | 复用 | 重算 |
| --- | --- | --- |
| conv2 仅 scale 变化 | conv1 | conv2, fc |
| conv2 仅校准样本数变化 | conv1 | conv2, fc |
| conv2 动态范围模式切换 | conv1 | conv2, fc |

## 追溯与业务码

追溯记录可重放为同一分数校验和与逐层校验和。验收覆盖以下错误路径：

| 业务码 | 观测字段 |
| --- | --- |
| `E_VERSION_UNSUPPORTED` | `field=version` |
| `E_FORMAT_CORRUPT` | `field=calibration_samples`；`field=zero_point` |
| `E_STALE_RESULT` | `layer=conv2`，同时带来源/当前量化版本 |
| `E_LINEAGE_BROKEN` | 缺失记录；篡改分数后在 `fc` 检测，偏差 `0.125` |
| `E_SEMANTIC_DRIFT` | 首个差异层 `conv2` |
| `E_SNAPSHOT_ROLLBACK` | 候选 v1 对已提交 v2 |
| `E_SNAPSHOT_CORRUPT` | `field=checksum` |
| `E_STAGE_BUSY` | 单写阶段占用时后到提交立即失败 |

## 语义等价与误差谱

v1/v2/v3 三个格式快照的分类分数校验和完全相同：

```text
598014a5e000beb94cc5249454950d12053419f8f3cdd669903b654bbd95e409
598014a5e000beb94cc5249454950d12053419f8f3cdd669903b654bbd95e409
598014a5e000beb94cc5249454950d12053419f8f3cdd669903b654bbd95e409
```

相对 float64 全精度参考的最大分类分数误差相同：`0.015412537121718639`。

v1→v2→v3 迁移链也通过同一等价裁决，三份快照保持相同分数校验和。

静态基线误差谱（逐层 p99）：

| 层 | 主导来源 | 输出误差 p99 | 放大比 |
| --- | --- | --- | --- |
| conv1 | weight_rounding | 0.010432605710608263 | 1.000000 |
| conv2 | weight_rounding | 0.02115773225374729 | 2.028039 |
| fc | weight_rounding | 0.015222062420821858 | 0.719456 |

逐通道 + 逐激活动态量化样例的误差谱同时包含非零 `per_channel_scaling` 与 `calibration_range` 统计，主导来源为 `per_channel_scaling`，可据此定位主要量化误差等级。

## 快照

- 初始整体提交版本：v2
- 回退 v1：`E_SNAPSHOT_ROLLBACK`
- 损坏校验和：`E_SNAPSHOT_CORRUPT`
- 单写占用：`E_STAGE_BUSY`
- 成功整体替换：v3
- 历史：`[2, 3]`
- 旧引用内容未改变，当前引用整体指向新快照

## GUI

当前容器设置了 `DISPLAY`，但 Qt `xcb` 平台插件缺少系统运行库，实际建窗失败；CLI 的 Qt 预检会安全返回状态 2，并提示改用无窗口路径，而不是让进程崩溃。

`QT_QPA_PLATFORM=offscreen` 下已验证：

```text
QT_OFFSCREEN_WINDOW_OK True 5 True
```

即窗口可创建、5 个诊断页签存在、验收状态为 PASS。交互视图包含逐层复用/重算、迁移补全/丢弃、误差谱、追溯/快照和最近错误码五个页签。

完整机器可读报告：`workspace_data/acceptance_report.json`。
