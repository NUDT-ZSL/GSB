# 独立诊断引擎技术说明

本交付不依赖 Docker 或外部服务，只使用 Python 3.10+、NumPy 和标准库。引擎在本地文件系统中完成确定性传播、块检查点、强制终止恢复、内容缓存、依赖失效、状态自检和 JSON 诊断报告。

## 快速命令

```bash
python3 -m pip install -r requirements.txt
python3 scripts/generate_sample.py --output samples
python3 -m unittest discover -s tests -v
python3 scripts/run_acceptance.py
python3 -m diagnostic_engine.cli self-check --state-dir workspace/acceptance/full_state
```

一键验收会真实启动子进程，在第 6 帧附近发送 `SIGKILL`，随后用新进程恢复，并生成 `workspace/diagnostic_report.json`。

也可用 CLI 复现定点失效：先对 `samples/tasks.jsonl` 运行一次，再对 `samples/revision_change.jsonl` 使用同一个 `--state-dir` 运行；后者会复用前 4 帧并重算第 4–11 帧。

## 目录与状态布局

- `diagnostic_engine/models.py`：JSONL 模型、输入校验、重复 ID、噪声数组维度和 revision 回退检查。
- `diagnostic_engine/noise.py`：按 `(noise_seed, clip_id, frame_index)` 派生的固定种子 NumPy 噪声。
- `diagnostic_engine/propagation.py`：显式整数传播算子 `timeline-propagation-v1`。
- `diagnostic_engine/cache.py`：缓存键构造、内容哈希命名 `.npy` 快照和严格回读校验。
- `diagnostic_engine/checkpoint.py`：单调版本检查点、临时文件 + `os.replace` 原子提交、`flock(LOCK_EX | LOCK_NB)` 非阻塞全局提交锁。
- `diagnostic_engine/resources.py`：块数、驻留字节、传播中间状态数上限和小块降级规划。
- `diagnostic_engine/engine.py`：分块生成、恢复、失效重算、统计和状态一致性自检。
- `scripts/generate_sample.py`：生成 `samples/tasks.jsonl`、`initial_noise_params.json` 和初始噪声帧 `.npz`。
- `scripts/run_acceptance.py`：固定种子端到端验收与最终 JSON 报告。
- `tests/test_diagnostic_engine.py`：23 个单元/集成测试，包含真实 `SIGKILL` 子进程恢复。

运行状态位于 `--state-dir`：

```text
checkpoints/<clip_id>.json
cache/keys/<cache_key>.json
cache/frames/<content_sha256>.npy
locks/commit.lock
tasks/<clip_id>.json
errors/latest_error.json
errors/error_log.jsonl
```

`workspace/` 已配置为所有者可读写/进入（目录权限 `755`）。原子替换要求临时文件与目标文件在同一目录，并依赖目录的写和进入权限。

## 确定性与逐帧一致

第 0 帧是固定种子噪声帧。第 `i > 0` 帧以前一帧的 `uint8` 快照为边界条件，执行 `propagation_steps` 次整数传播子步。每个帧边界都落盘为 `uint8`，恢复时从磁盘加载的前一帧与不间断运行喂给下一步的帧具有相同 dtype、shape 和字节内容，因此分块、崩溃恢复和一次性生成可逐字节相同。

逐帧哈希使用 `dtype + shape + C-contiguous bytes` 的 SHA-256。报告同时输出每帧 `sha256` 与对应 `cache_key`。

## 检查点与恢复

每个 block 全部帧生成后，引擎逐帧回读缓存并核对内容哈希，然后构造包含以下字段的检查点：

- 单调递增 `version`，首次提交为 `1`；
- 连续已完成帧集合 `completed_frames`；
- `frame_hashes` 与 `cache_keys`；
- 每个 block 的范围、块校验和、传播状态数；
- `aggregate_checksum`；
- 任务参数与资源规划；
- 最终完成时的重算/复用集合。

提交时先写同目录临时文件，`flush + fsync` 后 `os.replace`，再 fsync 目录。部分 block 不会推进检查点。重启时只信任最近一次原子检查点；检查点之后的缓存键记录视为未提交中间产物并删除，内存传播状态随原进程终止而消失，随后从该 block 起点重建。

版本不符的旧提交得到 `E_CHECKPOINT_CONFLICT`。全局提交锁使用非阻塞 `flock`；竞争失败者立即得到 `E_STAGE_BUSY`，不等待。进程被 `SIGKILL` 后内核释放 `flock`，不会留下无法恢复的锁。

## 缓存键与失效

缓存键材料包含：

- `clip_id`、`noise_seed`、`revision_epoch`；
- `frame_range=[i,i+1]`、`frame_index`、`block_range`；
- `block_size`、`propagation_steps`、`motion_mode`、`frame_shape`；
- `upstream_hash`、当前帧 `input_noise_hash`；
- 显式 `operator_version`。

键材料经规范化 JSON（键排序、无时间戳、无随机路径）后计算 SHA-256。缺少必要上游哈希返回 `E_CACHE_KEY_INVALID`。

修订事件可用可选字段 `change_frame` 指定依赖变更点；规划器将其对齐到所在 block 起点，受影响集合为该 block 至序列末尾，复用集合为之前的连续前缀。未提供 `change_frame` 时，全局 `revision`、`noise_seed` 或 `propagation_steps` 变更从第 0 帧开始失效。规划器强制：

```text
recomputed ∩ reused = ∅
recomputed ∪ reused = [0, total_frames)
```

违反时返回 `E_INVALIDATION_MISMATCH`。

## 资源上限

`ResourceLimits` 可配置：

- `max_blocks_per_clip`：单 clip 最大 block 数；
- `max_resident_bytes`：块帧缓冲 + 当前帧 + int16 传播状态的驻留字节预算；
- `max_propagation_states`：单 block 允许产生的传播子状态总数；
- `policy="degrade" | "reject"`。

内存或状态预算超限时，默认把 `effective_block_size` 缩小到满足预算的帧数，并在报告中标记 `resident_memory` 或 `propagation_states` 降级类别。block 数上限为硬限制；降级后 block 数仍超限、单帧无法满足内存下限或 `propagation_steps` 超过状态上限时返回 `E_RESOURCE_LIMIT`。

## 自检与错误码

`DiagnosticEngine.self_check()` 会验证所有缓存记录的键哈希、内容哈希和产物文件，再逐个检查检查点版本、连续完成帧集合、缓存引用、上游依赖哈希和最终重算/复用分区。发现偏差时返回 `E_STATE_INCONSISTENT`，`details.first_deviation_frame` 和 `details.checkpoint_version` 指明首个偏差帧与检查点版本；自检不会静默改写状态。

| 场景 | 业务码 |
| --- | --- |
| `block_size` / `propagation_steps` 非法 | `E_PROPAGATION_INVALID` |
| 重复 `clip_id` | `E_DUPLICATE_CLIP_ID` |
| 噪声数组维度不符 | `E_NOISE_SHAPE_MISMATCH` |
| revision 回退 | `E_REVISION_ROLLBACK` |
| 检查点版本冲突/旧版本覆盖 | `E_CHECKPOINT_CONFLICT` |
| 已提交帧无法恢复 | `E_RECOVERY_MISMATCH` |
| 缓存键缺少上游哈希 | `E_CACHE_KEY_INVALID` |
| 重算/复用集合遗漏或重叠 | `E_INVALIDATION_MISMATCH` |
| 状态自检不一致 | `E_STATE_INCONSISTENT` |
| 并发提交竞争失败 | `E_STAGE_BUSY` |
| 资源上限超限 | `E_RESOURCE_LIMIT` |

## 最终报告

`workspace/diagnostic_report.json` 由验收脚本生成，包含：

- 真实 `SIGKILL` 的返回码（Linux 通常为 `-9`）；
- 恢复前后检查点版本与恢复报告；
- 与不间断运行的数组/字节哈希一致性；
- 每个样本 clip 的逐帧校验和、缓存键和最终检查点版本；
- 定点修订的重算/复用帧集合；
- 全部错误码验收矩阵；
- 单元测试实际输出尾部；
- `latest_error` 最近业务错误。
