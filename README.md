# 多语种节奏挑战判定内核（`rhythm_kernel`）

仅使用 Python 3 标准库实现的可独立复核判定内核，面向广播互动音乐节目的
多语种节奏挑战环节。内核完成工程装载、按通道往返延迟校正、逐拍判定、
联动触发序列、多语种统计、增量重算、快照持久化与本地 HTTP 诊断视图。

## 目录结构

```
rhythm_kernel/
  model.py        # 常量与纯函数：中位数、最近节拍、窗口边界、规范哈希
  correction.py   # 语种|通道 RTT 校正量（median(RTT)/2）
  judgment.py     # BPM 动态窗口与逐拍判定（PERFECT/GOOD/OFFSET/MISS）
  validation.py   # 合法范围校验、环节归属、问题分级与稳定排序
  triggers.py     # 节拍提示音/点歌联动触发序列与环节钳制
  statistics.py   # 通道、语种、整体参与度/准确率/平均绝对偏差
  consistency.py  # 跨语种时间对齐一致性报告（可定位通道/语种）
  incremental.py  # 局部修改的增量重算与影响面范围
  persistence.py  # 快照持久化与逐事件 SHA-256 完整性校验
  reference.py    # 独立参考判定器（独立代码路径，交叉复核）
  generator.py    # 固定种子的确定性样本工程生成器
  server.py       # 仅监听 127.0.0.1 的 http.server 诊断视图
  cli.py          # generate / judge / incremental / resume / serve
tests/test_kernel.py
run_demo.py       # 端到端演示
artifacts/        # 随初始工程提供的参考工程与参考判定产物
```

## 判定规则（全部可复盘）

- **通道校正**：`one_way = median(rtt_samples_ms) / 2`，
  `corrected_click_ms = raw_click_ms - one_way`；无 RTT 画像的通道给出
  可识别的 `event.unknown_channel_group` 错误且不产生校正时刻。
- **最近节拍**：取绝对时间距离最小的节拍，等距时选择更早（下标更小）的拍。
- **动态窗口**：节拍间隔 `interval = 60000 / bpm`，四个上界为
  `interval * window_ratios`，上界全部含：
  `|deviation| <= perfect → PERFECT`，依次 `GOOD`、`OFFSET`，否则 `MISS`。
- **合法性校验**：原始点击、提交时刻、校正后时刻均须落在
  `legal_time_range_ms` 内；未知歌曲、未知通道、无环节归属分别标注错误，
  问题按错误优先、再按 code 稳定排序。
- **环节归属**：优先用校正后点击匹配声明的时间线环节；点击落在所有环节
  之外时回退到映射节拍所属环节并给出 `event.segment_from_mapped_beat` 警告。
- **联动触发**：每个有效判定产生 `beat_cue`（偏好拍点）与
  `song_request`（偏好 `max(校正时刻, 拍点)`），触发时刻钳制在所属环节
  `[start_ms, end_ms]` 内，保证不早于拍点、不跨越环节结束；同一环节内按
  逻辑时刻、触发类型、事件输入顺序稳定排序。
- **统计与标记**：按语种统计参与度、准确率（PERFECT+GOOD 占已判定数）
  与平均绝对偏差；相对全体基准严格超出
  `deviation_thresholds` 的语种进入 `flag_reasons`。
- **一致性报告**：通道校正中位数对比语种中位数、语种中位数对比全局中位数，
  超出 `consistency_threshold_ms` 即生成可定位到具体通道或语种的告警，
  并附带校正后越界事件。

## 增量与快照

- 事件新增/修改只重判该事件；节拍网格修改只重判请求该歌曲的事件，并在
  `affected_beat_ranges` 中报告覆盖的节拍下标与时间区间。
- 未受影响结果对象原样复用，保证逐字段不变；增量结果与全量重算在去除
  `state_revision`、`incremental_history` 记账字段后完全相等。
- 快照内嵌工程视图、统计、触发、校验、一致性报告及逐事件 SHA-256；
  加载时重算全部摘要，任何被篡改的区块都会抛出完整性错误。

## 使用方式

```bash
# 端到端演示（生成 -> 全量 -> 增量 -> 快照重启 -> 参考复核）
python3 run_demo.py

# CLI 分步执行
python3 -m rhythm_kernel.cli generate --project-out work/project.json
python3 -m rhythm_kernel.cli judge --project work/project.json \
    --results-out work/results.json --stats-out work/stats.json \
    --snapshot-out work/snapshot.json
python3 -m rhythm_kernel.cli incremental --project work/project.json \
    --changes work/changes.json --results-out work/inc.json \
    --scope-out work/scope.json
python3 -m rhythm_kernel.cli resume --snapshot work/snapshot.json \
    --changes work/new.json --results-out work/resume.json \
    --scope-out work/resume_scope.json --snapshot-out work/snapshot_v2.json
python3 -m rhythm_kernel.cli serve --project work/project.json --port 8080

# 测试（含与参考产物逐字段比对、独立参考判定器容差比对）
python3 -m unittest discover -s tests -v
```

诊断视图默认监听 `http://127.0.0.1:8080/`，以时间线展示每位听众的命中、
偏差与校正量，并在 `/api/consistency` 提供跨语种对齐一致性的机读报告。

## 验收对照

- `tests/test_kernel.py` 将内核输出与 `artifacts/results.json` 逐字段比较，
  并把快照摘要与 `artifacts/snapshot.json` 的逐事件 SHA-256 比较。
- `rhythm_kernel/reference.py` 是不共享生产流水线代码的独立参考判定器，
  在 `policy.float_tolerance_ms`（0.001 ms）内容差为零。
- 增量测试同时验证“未受影响部分逐字段不变”和“增量结果等于全量重算”。
