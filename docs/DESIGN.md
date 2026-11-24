# 技术设计

## 1. 输入模型

输入由四个本地 JSON 文件组成：

- `state.json`：定点标度、残差容差、状态键及定义域/初值。
- `story.json`：场景、事件、分支、优先级、前置条件与副作用。
- `storyboard.json`：画布、场景模板、矩形/文字/高亮层、状态键标记框。
- `run.json`：玩家分支选择和最大记录数。

状态键分两类：

- `fixed`：用 `int` 微单位保存；默认标度 `1_000_000`，代表 `0.000001`。
- `bool`：持有物品、已揭示线索、节点标志等。

校验在创建任何目录前完成。场景 ID、事件 ID、分支 ID 全局唯一；分支 `next` 和 `start_scene` 必须存在；条件、副作用和分镜标记的状态键必须已声明；定点初值、比较阈值和 `set` 值必须在定义域内；分镜矩形必须为正尺寸且完全落在画布内；`gray` 为 `0..255` 或 `#RRGGBB`；逻辑起始时刻必须为非负整数。

## 2. 定点与参考路径

定点运算：

1. 用 `Decimal` 读取声明值，避免 Python JSON float 的二次舍入。
2. 乘以标度后以 `ROUND_HALF_EVEN` 量化为整数微单位。
3. 比较、累加和定义域检查全部使用整数。

float64 路径：

- 初值取声明十进制文本的 `float`。
- 前置条件阈值取声明阈值文本的 `float`。
- 副作用按声明顺序 `+= float(声明值)` 或直接赋值。
- 不参与事件选择、分支可用性、状态写入或最终可达性。

每个数值前置条件和每个数值副作用都产生诊断点，记录：

```text
frame_index, scene_id, event_id/branch_id, phase,
state_key, fixed_value_micros, fixed_value,
reference_value, absolute_error, tolerance, exceeded_tolerance
```

`absolute_error` 使用 `Decimal.from_float(reference_value)` 与定点十进制值相减，避免只比较打印字符串。第一次 `absolute_error > tolerance` 的点写入 `first_exceeded`。每个条件同时记录 `fixed_passed` 与 `reference_passed`；若不一致，写入 `reference_mismatches`，但这不会改变执行路径。

## 3. 确定性推演

场景入口写一条 `scene_enter` 因果记录。之后反复执行：

1. 对所有尚未处理的事件按 `(-priority, declaration_index)` 排序。
2. 用定点当前世界状态逐条检查前置条件。
3. 选择第一条可触发事件，按声明顺序应用副作用。
4. 重新检查当前状态，直到没有可触发事件。
5. 选择运行时指定的分支；未指定时使用唯一可用分支或 `default`。
6. 应用分支副作用，进入 `next` 场景或结束。

前置条件不读取历史票据，只读取当前世界状态。每次状态键实际变化都写入账本，包含：

- `record_id`、`frame_index`、`logical_time`、`scene_id`
- `event_id` / `branch_id`、优先级
- 被满足前置条件及定点/参考判定值
- 状态变更前后值、定点判定值
- `causal_parent_record_id`
- `scene_sequence` 与 `upstream_choices`

场景入口帧时刻为 `scene.start_time`；第 N 个事件/分支记录为 `start_time + N`。帧规范直接由这些真实账本记录生成。

## 4. 灰度帧

渲染器完全使用 NumPy，避免字体服务和 Qt 图形栈带来的平台差异：

- 矩形层：直接写入灰度值。
- 文字层：使用内置 5x7 位图字体，按整数倍放大。
- 高亮层：确定性斜纹 + 边框。
- 事件标记：按分镜标记框绘制，携带 `record_id`、事件/分支 ID、状态键和定点值。

每个帧块包含若干帧元数据、`width`、`height`、原始像素 Base64 和像素 SHA-256。帧块按 `(scene_id, block_index)` 规划并赋予稳定序号。像素内容与线程调度顺序无关。

界面启动后由后台线程恢复或生成帧块；UI 线程只读取已收到的帧。时间轴上未完成的帧使用固定的 `PENDING` 棋盘占位图，不执行同步磁盘等待。

## 5. 快照与摘要

每类快照有独立清单 `manifest.json`：

```text
format, kind, version_id, run_id, entries, digest
```

每个条目记录 `unit_key`、稳定 `sequence`、文件名、内容摘要和 `entry_count`。快照文件是：

```text
format, kind, version_id, run_id, unit_key,
sequence, entry_count, payload, digest
```

摘要为规范化 JSON 的 SHA-256。清单也计算自身摘要。恢复时同时检查：

- 清单身份、摘要、条目唯一性
- 快照身份与清单记录
- 快照内容摘要
- 声明条目数与实际 ledger/frames 长度
- 每帧像素摘要、尺寸和字节数
- 未被清单引用的孤儿文件

任何失效项都形成 `E_DIGEST` 记录；绝不静默采用。模拟恢复选择序号最大的有效快照；若最新失效则回退到次新有效快照；若没有有效快照则归档当前失效运行并从头开始。帧恢复加载所有有效块，失效块重新渲染。

内容版本 `version_id` 是状态、剧本和分镜规范化内容的摘要；`run_id` 还包含分支选择与最大步数。发现新版本时，`prepare()` 将旧版本快照目录整体移入 `<state-dir>/archive/`。

强杀可能落在“快照文件已写、清单尚未更新”的窗口。此时文件是摘要有效的孤儿，但不被清单接受；恢复会报告 `E_DIGEST`，并从较旧快照继续。

## 6. 中断边界

事件任务每次最多推进一条账本记录。主调度线程在返回后原子写快照和清单，再检查中断标志。

帧块并发时：

1. 停止提交新块。
2. 等待已在途块完成并落盘。
3. 取消尚未开始的任务。
4. 在最后完成的帧块边界写中断标记并退出。

`SIGINT -> 130`，`SIGTERM -> 143`。`SIGKILL` 不可处理，只能依赖已落盘边界。完成结果最后写入，因而中断或强杀不会产生伪完成。

## 7. 并发与内存预算

`BoundedTaskPool` 用 `ThreadPoolExecutor + BoundedSemaphore` 限制提交窗口。事件任务经同一线程池串行提交，保持场景内顺序；帧块在预算允许时并行。

`MemoryBudget.try_reserve` 在提交前预留：

- 事件任务：`state_buffer_bytes`
- 帧任务：完整画布像素、Base64 膨胀和元数据估计

若预算不足，主调度器先收割已完成任务、落盘并释放，再重试预留；不会缩小画布、灰度位深、帧块或定点标度。若单个完整帧块也大于预算，立即 `E_BUDGET`。

帧任务异常时记录首个异常，取消其余 future，等待已启动任务结束，释放全部预算，再抛出 `E_TASK`。

## 8. 一致性校验

从账本重新生成期望帧标记，与每个持久化帧的以下字段逐一比对：

- `record_id`
- `event_id` / `branch_id`
- `state_key`
- `fixed_value`
- `value_kind`
- 帧号、逻辑时刻、场景、记录类型

发现偏差时，报告首个不一致的帧号、场景、状态键、期望值与实际值，并返回 `E_REPLAY`。通过时同样生成 `replay_report.json`，其中 `trace` 把帧号、逻辑时刻、节点 ID 和账本记录 ID 串起来。

## 9. 界面线程模型

`RunnerThread` 是独立 `QThread`。快照 I/O、模拟、帧渲染和校验都在其中执行。进度通过 Qt signal 返回 UI 线程：

- 模拟边界：增量加入因果表。
- 帧块完成：把已持久化像素加入内存帧缓存。
- 完成：加载完整结果、诊断和一致性报告。

拖动时间轴只查内存帧缓存；未命中时生成确定性占位图。因此界面不会因磁盘或未完成任务阻塞。

## 10. 可验证性

固定样本覆盖：

- 定点与 float64 在阈值处产生相反判定
- 动态解锁的高优先级事件
- 分支选择与场景序列
- 多场景分镜模板和标记
- 多帧块并发与预算等待
- 摘要篡改、版本归档、事件边界中断、强杀恢复

