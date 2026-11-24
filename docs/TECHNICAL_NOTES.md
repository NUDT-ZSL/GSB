# 离线骨骼动画重算组件技术说明

## 1. 运行方式

推荐使用 Python 3.11 虚拟环境：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install numpy
.venv/bin/python -m animrecalc.cli generate-samples --out samples
.venv/bin/python -m animrecalc.cli validate --sample-dir samples
.venv/bin/python -m animrecalc.cli baseline --sample-dir samples --state-dir artifacts/state
.venv/bin/python -m animrecalc.cli incremental --sample-dir samples --state-dir artifacts/state
```

一键验收：

```bash
.venv/bin/python scripts/run_acceptance.py
```

它会运行 `tests/test_acceptance.py`，并生成：

- `artifacts/acceptance_report.json`
- `artifacts/acceptance/unittest.log`
- `docs/validation-report.md`

## 2. 输入模型

样本由固定种子 `20261125` 生成在 `samples/`：

- `skeleton.json`：骨名、父骨、静止 TRS、骨段长度。
- `clips.json`：片段时长、循环次数、每骨每通道关键帧。
- `state_machine.json`：声明参数、状态/片段映射、优先级、打断条件。
- `edit_script.json`：有序增删改算子。
- `chunk_spec.json`：整数毫秒步长、每块帧数、容差、并发和内存预算。

支持通道：

- `tx`、`ty`、`tz`：标量线性插值。
- `translation`、`scale`：三分量线性插值。
- `rotation`：四元数 `[w,x,y,z]` 球面插值。

允许的编辑算子见 `animrecalc/edits.py:ALLOWED_OPERATIONS`，包括 `add_key`、`upsert_key`、`update_key`、`update_key_value`、`delete_key`、`set_clip_duration`，以及转移和条件的增删改。条件比较符使用 `comparison` 字段，避免与编辑算子的 `op` 字段冲突。

## 3. 确定性求值

时间轴只使用整数逻辑毫秒和整数帧号：`time_ms = frame_index * step_ms`。

求值分两阶段：

1. `build_state_trace()` 顺序生成每帧状态、片段、局部时间、循环序号、完成标记和转移事件。转移按 `priority` 降序、ID 字典序决胜。
2. `evaluate_range()` 使用第一阶段轨迹采样通道，生成局部 TRS，再按骨架拓扑序执行 `parent_global @ local`。

事件来自状态轨迹，分块中的 `events` 是同一轨迹的切片，不是事后从姿态反推。事件包含 `frame`、`time_ms`、`source_state`、`target_state`、`source_clip`、`target_clip`、`transition_id`、`priority` 和 `decisive_condition`。

`entry_blend_ms > 0` 时，进入片段后的短窗口内对来源片段和目标片段的局部 TRS 做线性/球面混合；为 `0` 时在进入帧直接吸附目标首帧。

## 4. 依赖图与失效闭包

每个分块持久化一份 `dependency`：

- `bone_channels[].span_start/span_end`：该块实际依赖的关键帧支撑区间。
- `clip_windows`：块内片段归属、片段起点、局部采样范围和循环序号。
- `transition_ids`、`state_names`：块内候选或触发的转移。
- `events`：块内事件。

失效规则：

- 关键帧/通道值：计算旧/新关键帧邻接区间的并集，映射到该片段局部时间对应的全局帧和块；再用骨架 `children` 闭包加入被改骨骼的全部后代。
- 片段时长：构造新旧状态轨迹，找到首个调度/采样分歧帧，只将该帧所在块及其后的 ripple 后缀标记为失效；这些块计入 `ported_chunks`。
- 转移条件/规则：标记依赖图中求值过该转移或处于来源状态的块；若新旧轨迹产生分歧，再加入首个分歧后的 ripple 集合。
- 摘要损坏、缺失或帧数不符：该块附加 `E_DIGEST` 理由并升级为全骨重算。

关键帧编辑只重算闭包内骨骼。干净父骨的全局矩阵从已验证块复用，脏骨按同一拓扑序重算；因此浮点运算顺序与全量求值一致，样本上最大差为 `0.0`。

## 5. 持久化与版本

分块路径为 `state/chunks/chunk_000000.json`。写入流程：

1. 计算除 `digest` 外规范 JSON 的 SHA-256。
2. 写入同目录唯一 `.<name>.*.tmp`。
3. `flush + fsync` 后 `os.replace()` 原子替换。
4. 替换前若旧块 `version_id` 不同，先移动到 `state/archive/<old_version>/chunks/`。
5. 更新 SQLite `state/catalog.sqlite` 目录表；恢复仍以摘要有效的 JSON 块为准。

全局 `version_id` 由骨架、片段轨道、状态机规则和采样口径的规范 JSON 计算。重启时：

- `state/version.json` 与输入版本相同：扫描所有块摘要和帧数，只补算无效/缺失块。
- 全局版本不同：所有块重算，旧块在替换前归档。
- 强制终止：临时文件不会被识别为完成块；重启只承认最终文件名且摘要有效的块。

显式摘要验证：

```bash
.venv/bin/python -m animrecalc.cli verify-chunk \
  --sample-dir samples --state-dir artifacts/state --chunk-index 5
```

摘要损坏时返回：

```json
{"ok": false, "error": {"code": "E_DIGEST", "...": "..."}}
```

## 6. 并发与内存预算

`BoundedChunkScheduler` 使用有界 `ThreadPoolExecutor`。调度器始终按块索引升序提交；当在途 worker 数达到 `max_workers`，或加入下一块会超过 `memory_budget_bytes` 时，等待已有块完成。等待次数记录在 `scheduler.memory_wait_events`，不会通过降低采样精度来腾出预算。

任一块抛错后设置停止事件、取消尚未开始的 future，并在报告中返回 `first_error`、`errors`、`successful_chunks` 和 `successful_chunk_paths`；已经原子写入的成功块保留。

## 7. 信号与 Windows 复现

CLI 安装 `SIGINT`、`SIGTERM`，Windows 上额外安装 `SIGBREAK`。信号只设置 `threading.Event`，调度器在提交下一分块前检查；已经在途的块会算完并原子落盘。停止后写入 `state/INTERRUPTED.json`，进程以约定退出码 `75` 退出。

确定性复现：

```bash
.venv/bin/python -m animrecalc.cli baseline \
  --sample-dir samples --state-dir artifacts/interrupt-demo \
  --workers 1 --interrupt-after 2
echo $?   # 75

.venv/bin/python -m animrecalc.cli recover \
  --sample-dir samples --state-dir artifacts/interrupt-demo
```

真实信号实验可使用隐藏测试钩子 `--chunk-delay-ms 250`：

```bash
.venv/bin/python -m animrecalc.cli baseline \
  --sample-dir samples --state-dir artifacts/signal-demo \
  --workers 1 --chunk-delay-ms 250 &
pid=$!
sleep 1
kill -TERM "$pid"
wait "$pid"; echo $?   # 75
```

Windows PowerShell 可运行 `tests/test_acceptance.py` 中的子进程用例；测试在 Windows 下以 `CREATE_NEW_PROCESS_GROUP` 启动，并发送 `signal.CTRL_BREAK_EVENT`。

## 8. 验证边界

当前输出姿态为每骨 4x4 全局矩阵的 16 个 `float64` 分量，同时块内保留事件和依赖。验收脚本比较：

- 姿态 shape、逐骨逐分量最大误差和首个超容差位置。
- 转移事件列表的完全相等。
- 重算块、复用块、时间边界移植块数量。
- 中断标记、恢复计数、摘要故障和旧版本归档。

最新实测数字见 `docs/validation-report.md`。
