# 多屏状态一致性技术说明

本实现只依赖 Python 标准库（含 `sqlite3`），运行环境实测为 Python 3.10.12；代码未使用 3.11 专有特性，可在 Windows Python 3.11 运行。

## 1. 目录与产物

- `multiscreen/generator.py`：固定种子 `394` 的确定性样本生成器。
- `multiscreen/validate.py`：会话清单、JSONL/JSON 事件流、时间轴换算校验。
- `multiscreen/engine.py`：逐接收端帧采纳状态机、等待缺口和代数切换重建。
- `multiscreen/merge.py`：v1 扁平 JSON 与 v2 JSON 分片/SQLite 元数据合并裁决。
- `multiscreen/digest.py`、`multiscreen/loader.py`：SHA-256 防篡改、有效快照集选择、有界并发加载和内存闸门。
- `multiscreen/replay.py`、`multiscreen/verify.py`：并发安全重放、逐字段裁决和 `E_REPLAY` 偏差定位。
- `multiscreen/persist.py`：原子分块、版本标识、中断标记、摘要有效前缀恢复和状态 SQLite。
- `multiscreen/pipeline.py`、`multiscreen/cli.py`：端到端编排与命令行。
- `scripts/acceptance.py`：一键本地验收并生成 `output/acceptance_report.json`。

生成后的样本直接位于 `task/`：

- `sessions.json`：2 个会话、3 个全局唯一接收端、几何/缩放/起点/容忍窗口。
- `frames.jsonl`：29 条事件，包含关键帧、增量帧、丢弃帧、丢序号、迟到采样和显式切换标记。
- `snapshots/v1/snapshot_v1.json`：相对起点秒数和扁平游标。
- `snapshots/v2/shard_000.json` 至 `shard_002.json`：每接收端一个分片。
- `snapshots/v2/meta.sqlite3`：v2 元数据库。
- `snapshots/v2-backup/`：摘要有效的最近备份分片集，用于篡改当前分片后的回退。
- `snapshots/digests.json`：每份文件的 SHA-256、分片接收端数和集合声明数。

## 2. 状态机规则

每个接收端独立维护以下核心字段：

- `adoption_cursor_seq`：最后连续采纳的帧序号。
- `keyframe_seq` / `keyframe_ms`：最近采纳关键帧恢复点。
- `playback_position_ms`：绝对逻辑毫秒播放位置。
- `generation`：当前会话代数。
- `open_gap` / `gap_intervals`：当前缺口和已关闭缺口区间。

裁决规则：

1. 关键帧不检查连续性和窗口，直接建立新解码基线，并关闭当前缺口。
2. 丢弃帧不推进游标，打开或延长等待缺口。
3. 增量帧必须同时满足：
   - 已有关键帧基线；
   - `frame_seq == adoption_cursor_seq + 1`；
   - 采样时刻位于 `playback_position_ms ± tolerance_ms` 窗口。
4. 不满足条件的增量帧进入 `gap_wait`，记录未见序号、已到达但被拒绝序号、缺口开始逻辑时刻和原因。
5. 后续关键帧或代数切换可以打断缺口；切换会把跨越切换点的缺口拆成切换前、切换后两个可审计区间。

同一接收端事件按 `event_seq` 归约；不同接收端之间无共享可变状态，因此并发度只影响调度，不影响结果。

## 3. 代数切换重建

切换触发源有三类：

- 事件流中的 `record_type: "switch"` 标记；
- 帧声明的 `generation` 与当前代数不同；
- 增量帧超出容忍窗口，且会话清单声明了 `breach_switch_generation`。

切换时调用 `rebuild_cursor_from_adoptions()` 这一纯规则函数，从最近采纳关键帧重建：

- `old_cursor`：旧代数游标、播放位置、关键帧和未闭缺口；
- `recovery_keyframe`：重建起点；
- `new_baseline`：若切换后收到新代关键帧，则为新代关键帧，否则暂时复用恢复关键帧并标记为 provisional；
- `discarded_increments`：关键帧之后曾采纳但被重建回卷的增量，以及切换前缺口内无效帧；
- `post_switch_discarded_increments`：切换后、新代关键帧前被缺口拒绝的增量；
- `closed_cross_switch_gap`：在切换点关闭的跨代缺口。

流式处理和离线重建共用同一纯函数；分块续跑通过持久化的 `keyframe_adoption` 与 `adopted_increments_since_keyframe` 保持同一规则。

## 4. 快照合并与冲突裁决

v1 的绝对时间按元数据换算：

```text
absolute_ms = session.start_ms + Decimal(relative_play_seconds) * 1000
```

使用 `Decimal(str(value))` 避免二进制浮点误差；换算后必须为整数逻辑毫秒。

规范接收端标识来自清单，v1 的 `FLAT-*` 名称通过 `aliases` 映射。裁决优先级：

1. 墓碑记录优先于非墓碑记录；
2. 非墓碑记录按会话代数取新；
3. 同代按绝对逻辑毫秒取新；
4. 完全同值时按 v2 来源和来源位置稳定破平。

每条游标、播放位置或代数冲突都输出 winner/loser 的 `source_set_id`、`source_shard`、`source_location`、原始接收端标识、代数、时间戳、值和裁决规则。

## 5. 摘要与回退

`digests.json` 为每个快照集合和文件记录 SHA-256。加载时核对：

- 文件是否存在；
- 文件摘要是否等于记录摘要；
- 分片声明接收端数是否等于 JSON 实际数量；
- 集合声明总数是否等于分片并集。

失败集合进入 `rejected_snapshots`，错误码为 `E_DIGEST`。编排层按 `preference` 和逻辑时间选择下一份有效集合；当前 v2 被篡改时回退到 `v2-backup`，v2 全失效时可回退 v1。没有有效记录时才向上抛出 `E_DIGEST`，绝不会静默使用失效分片。

## 6. 分块持久化与中断恢复

每个分块写入 `state/chunk_XXXXX.json`，外层包含：

- `version_id = sha256(规范化 manifest.sessions + events)`；
- `index`；
- `body_digest`；
- `body`：逻辑时间范围、事件序号范围、轨迹、重建报告、计数和逐接收端状态。

写入方式是同目录 `*.tmp` → `fsync` → `os.replace()`。`merged_view.json` 和 `report.json` 也使用相同原子替换。`metadata.sqlite3` 在首次运行时创建，用于登记 run 和 chunk 元数据。

`SIGINT`/`SIGTERM` 只设置线程安全标志；流水线仅在分块边界检查。优雅中断会写 `_interrupted.json`，约定退出码 `130`。重启时：

1. 计算同一输入的 `version_id`；
2. 按序号读取连续分块；
3. 遇到缺失、截断、JSON 损坏、版本不符或摘要错误即停止；
4. 只恢复此前的有效前缀；
5. 从下一分块继续重放并覆盖损坏的边界分块。

被 `kill -9` 后可能留下 `.tmp` 或不完整文件；恢复逻辑不把 `.tmp` 当成分块，官方分块若损坏则按摘要截断前缀，且不承诺已有退出报告。

## 7. 并发与内存预算

`OrderedMemoryGate` 按接收端/分片标识的字典序分配 ticket：

- 键顺序固定；
- 队首预算不足时等待，后续键不能插队；
- 单个数据块超过总预算直接返回 `E_BUDGET`；
- 校验精度、摘要比对和字段数量不会因预算降低。

分片加载使用 `ThreadPoolExecutor(max_workers=configured)`。任一分片失败会设置取消标志、取消队列中的其他 future，并抛出按分片顺序确定的首个业务错误。重放按接收端并行；不同 worker 数的 `results`、`trace` 和 `rebuilds` 规范化 JSON 完全一致。

## 8. 错误码

- `E_SCHEMA`：JSON/SQLite 结构或必填字段非法。
- `E_DUP_SESSION` / `E_DUP_RECEIVER`：标识重复。
- `E_GEOMETRY`：宽高或缩放比不是有限正数。
- `E_CLOCK`：逻辑毫秒或起点不是非负整数。
- `E_FRAME_ORDER`：同接收端帧序号未按事件序号严格递增。
- `E_FRAME_TYPE`：帧类型与关键帧标记矛盾。
- `E_REFERENCE`：事件或快照引用了未知会话/接收端。
- `E_TIMELINE`：v1 秒数无法换算到整数绝对毫秒。
- `E_DIGEST`：摘要、缺失文件或接收端数核对失败。
- `E_REPLAY`：重放状态与合并视图不一致。
- `E_BUDGET`：单块超过内存预算或预算参数非法。
- `E_INTERRUPT`：分块边界收到终止请求，退出码 130。

## 9. 常用命令

Linux/macOS：

```bash
python3 scripts/generate_samples.py
python3 -m multiscreen validate --data task
python3 -m multiscreen run --data task --state state --report-out output/report.json --workers 4 --chunks 4
python3 -m multiscreen recover --data task --state state
python3 -m unittest discover -s tests -p 'test_*.py' -q
python3 scripts/acceptance.py
```

Windows PowerShell 将 `python3` 换成 `py -3.11`，路径分隔符可使用 `\`：

```powershell
py -3.11 scripts\generate_samples.py
py -3.11 -m multiscreen run --data task --state state --report-out output\report.json
py -3.11 -m unittest discover -s tests -p "test_*.py" -q
```

## 10. 实测结果

最近一次本地 `python3 scripts/acceptance.py` 与 `pytest`：

- `16 passed`，验收 JSON `ok=true`（本机完整 pytest 约 0.5 秒）。
- 正常样本：29 条事件、3 个接收端、8 条冲突、3 条裁决来源记录。
- 采纳计数：关键帧 6、增量 11、丢弃帧事件 1、缺口等待 10、切换标记 1。
- 代数重建：2 次；分别为侧屏显式标记 `1 -> 2`、投影端容忍超限 `1 -> 2`。
- 持久化：4/4 分块恢复，分块终态与全量重放逐字段一致。
- 并发：workers=1 与 workers=4 的结果规范化摘要一致。
- 中断：写入 2 块后以退出码 130 中断；续跑从 chunk 2 开始，最终恢复 4 块并通过。
- 防篡改：篡改 `v2/shard_000.json` 后记录 `E_DIGEST`，回退 `v2-backup`，验收仍通过。
- 偏差样本：游标改为 999 后输出 `E_REPLAY`，首个偏差为 `rcv-side@1000ms/adoption_cursor_seq`，expected `999`、actual `1`。

主要报告：

- `output/acceptance_report.json`
- `output/report.json`
- `output/report_digest_fallback.json`
- `output/report_replay_mismatch.json`
- `output/report_interrupt_resume.json`
- `output/error_invalid_input.json`
- `output/pytest-run.txt`
