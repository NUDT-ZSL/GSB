# 离线多屏投屏诊断组件说明

## 1. 交付边界

本原型只使用 Python 标准库，SQLite 使用 `sqlite3`，Tkinter 使用标准库 `tkinter`。不访问局域网、网络服务或真实设备；所有样本由固定种子 `384` 确定性生成。

推荐环境为 Windows + Python 3.11。当前工程中的命令同时兼容 PowerShell、bash 和 cmd 路径写法。

## 2. 目录结构

- `screen_diag/sample_generator.py`：生成会话清单、帧事件流、v1 JSON、v2 JSON 分片与 SQLite 元数据库、只读注入清单。
- `screen_diag/validation.py`：严格 JSON/Schema 校验与 v1 相对秒换算。
- `screen_diag/replay.py`：确定性采纳裁决、代数回退、关键帧重建和逐屏重放。
- `screen_diag/snapshots.py`：SQLite 元数据读取、有界线程池/内存预算分片加载、SHA-256 摘要校验和两代快照合并。
- `screen_diag/compare.py`：合并视图与重放结果的逐时刻、逐屏、逐字段比对。
- `screen_diag/persistence.py`：原子分块、链式摘要、中断标记、信号处理和有效前缀恢复。
- `screen_diag/pipeline.py`：统一编排、报告和退出码。
- `screen_diag/gui.py`：Tkinter 本地诊断视图和分片跳转。
- `screen_diag/cli.py` / `screen_diag/__main__.py`：命令行入口。
- `tests/test_diagnostic.py`：14 个 `unittest` 用例。
- `scripts/validate.ps1`：Windows PowerShell 验收脚本。
- `scripts/validate.sh`：Linux/macOS 等价验收脚本。

## 3. 样本构成

固定样本包含两个同时投屏会话：

- `sess-home-living-s1`：手机发送端到客厅大屏 `canon:display:living-room-big`，协议 S1，几何 `3840x2160`，缩放 `2.0`。
- `sess-meeting-secondary-s2`：同一手机发送端到会议室副屏 `canon:display:meeting-room-secondary`，协议 S2，几何 `1920x1080`，缩放 `1.0`。

每屏 16 个事件，两屏共 32 个事件：

- 28 个正常采纳；
- 2 个迟到丢弃；
- 2 个旧代数回退。

帧事件额外包含可选 `generation` 字段。若外部事件流不提供该字段，解析器按第 1 代处理；固定样本用它显式覆盖新代数、迟到旧采样和旧代数回退。

## 4. 裁决与重放规则

对每个接收屏独立维护：

- 当前会话代数 `generation`；
- 最近关键帧序号 `keyframe_seq`；
- 已解码帧序号 `decoded_frame_seq`；
- 播放位置 `play_position_ms`；
- 已观察到的最高整数逻辑毫秒时钟 `clock_ms`。

裁决严格按 `frame_events.json` 的声明顺序执行，不在线程池中重放事件：

1. `late_by_ms = max(0, clock_ms - sampled_at_ms)`。
2. 若 `late_by_ms > late_tolerance_ms`，裁决为 `late_dropped`，不改变解码基线。
3. 若事件 `generation < 当前 generation`，裁决为 `generation_rollback`；向前查找同屏同代最近关键帧，从该关键帧顺序重建到触发事件。
4. 否则裁决为 `adopted`；关键帧到达时把 `keyframe_seq` 和 `decoded_frame_seq` 一起重置到该关键帧序号。
5. 播放位置为 `sampled_at_ms - session.start_logic_ms`，所有时间均为整数逻辑毫秒。

同一输入两次重放的完整状态序列、裁决轨迹和计数经规范化 JSON 摘要比较，测试保证逐字段一致。

## 5. v1/v2 合并

- v1 是扁平 JSON，只含相对起始秒；换算公式为 `start_logic_ms + relative_second * 1000`。为满足整数逻辑毫秒轴，小数秒乘 1000 后必须仍是整数毫秒。
- v1 不含代数，工具显式按 `v1_implicit_generation = 1` 处理，并在报告的 `merge.version_identification` 中标明。
- v2 使用规范屏标识、绝对逻辑毫秒、会话代数、关键帧序号、解码帧序号、播放位置、墓碑和分片摘要。

同一 `(canonical_screen_id, logic_ms)` 分组的裁决优先级：

1. 墓碑优先于非墓碑记录；
2. 否则按 `(generation, logic_ms)` 取新；
3. 两者仍相同则用确定性的来源类型、路径排序兜底，并标记 `deterministic_source_tie_break`。

关键帧序号、播放位置或代数不一致时，每个字段生成一条冲突记录，包含所有候选值、胜者来源、败者来源、`shard_id`、JSON 定位符和规范屏标识。

## 6. E_SCHEMA、E_DIGEST、E_REPLAY

| 状态 | 退出码 | 含义 | 是否写报告/状态 |
| --- | ---: | --- | --- |
| `PASS` | 0 | 摘要有效且重放比对通过 | 是 |
| `E_SCHEMA` | 2 | 输入结构、几何、序号、时间或秒换算非法 | 否，先全量校验后创建输出 |
| `E_DIGEST` | 3 | 分片摘要不匹配/缺失、分片缺失或声明屏数不符 | 写诊断报告；失效分片不进入合并 |
| `E_REPLAY` | 4 | 合并视图与重放状态存在字段偏差 | 写含首个偏差的报告 |
| `E_INTERRUPTED` | 130 | 终止信号或确定性边界注入在分块边界生效 | 写中断标记和已完成分块 |

`E_SCHEMA` 错误以 JSON 写到 stderr，便于脚本解析；在校验通过前不会创建 `--report`、`--state-dir` 或其他诊断输出。异常注入器会先复制样本到可变目录，因此原始 `samples/` 不被修改。

## 7. 防篡改与回退

每个 v2 分片的摘要是对其规范化 JSON 文档计算的 SHA-256：

```text
sha256(canonical_json(shard_document))
```

SQLite `shards.digest` 保存记录摘要。加载时同时校验：

- 文件存在且 JSON 可解析；
- 实际摘要等于数据库记录摘要；
- 记录摘要非空；
- `declared_screen_count` 等于分片中不同 `canonical_screen_id` 的数量。

任一失败都会产生 `E_DIGEST` 失败项。失效分片的记录不会进入合并；`shard_fallbacks` 为其中每个接收屏定位最近的、摘要有效的前序分片。若更早没有有效分片，则明确报告 `available=false`，不会静默补值。

## 8. 分块持久化与中断恢复

状态目录包含：

- `chunk-manifest.json`：输入版本 ID、分块顺序、路径、SHA-256 和前一块摘要；
- `chunks/chunk-XXXX.json`：原子替换写入的分块信封；
- `INTERRUPTED.json`：边界中断标记。

版本 ID 为：

```text
sha256(sha256(canonical_manifest) + ":" + sha256(canonical_ordered_events))
```

事件列表顺序参与摘要，因为声明顺序本身是裁决语义的一部分。

每个分块包含该逻辑时间区间内的裁决轨迹、合并点和边界屏状态。写入采用临时文件、`flush + fsync`、`os.replace` 和清单原子更新。恢复时从清单顺序验证文件摘要、清单摘要、链式 `previous_digest` 和版本 ID；遇到第一个失效块即停止，只返回连续有效的前缀。`--resume` 会截断失效后缀并重算后续块。

`SIGINT`、`SIGTERM` 和 Windows 可用时的 `SIGBREAK` 只设置停止标志。若信号在分块写入期间到达，当前块写完后才停止；若在两块之间到达，则不写下一块并立即写中断标记。约定退出码为 `130`。`--interrupt-after-chunks N` 提供不依赖 shell 时序的确定性验收方式。

## 9. 并发与内存预算

`load_v2_snapshot(max_workers=N, memory_budget_bytes=B)` 使用 `ThreadPoolExecutor` 和有界在途字节数：

- 分片先按 `(最小 canonical_screen_id, shard_id)` 排序；
- 提交前若达到并发上限或内存预算，等待最先完成的 Future；
- 预算不足时记录 `budget_waits`，等待顺序与接收屏标识排序一致；
- 单个分片大于预算时，排空在途任务后单独加载该分片，保证不降低校验精度。

分片校验在线程中执行，帧裁决和重放仍在主线程按声明顺序执行，因此并发不会改变裁决轨迹。

## 10. Tkinter 视图

Windows 标准 Python 3.11 自带 Tkinter。启动：

```powershell
py -3.11 -m screen_diag gui --samples samples
```

或双击 `run_gui.cmd`。界面包含：

- `Screens`：当前每屏代数、关键帧、解码帧、播放位置和来源分片；
- `Conflicts`：冲突字段、候选值、裁决方式和来源定位；
- `Decisions`：逐事件采纳/迟到/回退轨迹；
- `Shards`：分片有效性、屏数、摘要和路径；
- `Report JSON`：完整报告。

双击屏幕、冲突或分片行可打开对应 JSON 分片，并可在 Windows 资源管理器中定位文件。
