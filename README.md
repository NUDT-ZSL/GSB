# 本地关卡约束求解引擎

从零实现的纯本地、确定性关卡约束求解器。输入 JSON 关卡规格，按约束声明顺序生成房间与走廊，支持冲突回退、分步快照、中断续算、版本漂移归档与摘要防篡改。

## 环境

- Python 3.10+
- Windows 10+ / Linux 均可
- `requirements.txt` 列出 `numpy>=1.24` 作为任务环境对齐项；核心求解与测试只使用 Python 标准库，不依赖 NumPy 全局随机状态、网络、数据库或外部数据。

```bash
python -m pip install numpy
```

## 快速开始

```bash
# 1. 生成确定性本地样本
python -m level_solver sample demo spec.json

# 2. 求解；成功退出码 0
python -m level_solver solve spec.json \
  --snapshot-dir snapshots --output result.json

# 3. 在约束边界主动中断；标准退出码 75，并写 interrupt.json
python -m level_solver solve spec.json \
  --snapshot-dir snapshots --interrupt-after 2

# 4. 从最新有效快照续算
python -m level_solver resume spec.json \
  --snapshot-dir snapshots --output resumed.json

# 5. 校验全部快照内容摘要与版本标识
python -m level_solver verify spec.json --snapshot-dir snapshots
```

也可使用 `python main.py ...`；Linux 环境通常将命令中的 `python` 换成 `python3`。

## 规格格式

```json
{
  "width": 30,
  "height": 20,
  "blocked": [[10, 0], [10, 1], [4, 5]],
  "seed": 20261115,
  "rollback_limit": 12,
  "attempt_limit": 16,
  "concurrency": 4,
  "memory_budget_kb": 512,
  "constraints": [
    {"id": "rooms", "type": "min_rooms", "count": 7},
    {"id": "area", "type": "max_area", "max_area": 12},
    {"id": "spacing", "type": "room_spacing", "min_spacing": 1},
    {"id": "connectivity", "type": "connectivity"},
    {
      "id": "reachability",
      "type": "start_end_reachable",
      "start": [0, 0],
      "end": [29, 19]
    }
  ]
}
```

- `width`、`height` 必须为正整数。
- `blocked` 使用 `[x, y]` 或 `{"x": ..., "y": ...}`，坐标必须位于网格内。
- `seed` 必须是整数。引擎由种子、约束序号、约束 id 与候选序号派生稳定 PRNG，不用 `random` 全局状态或系统时间。
- `rollback_limit` 为非负整数，默认 `3`。
- `attempt_limit` 为每个约束的候选尝试上限，正整数，默认 `12`。
- `concurrency` 为预检线程数，范围 `1..128`，默认 `1`。
- `memory_budget_kb` 为候选状态内存预算，默认 `65536` KiB。至少需容纳基础状态与一个候选状态，否则返回 `E_RESOURCE`。
- `snapshot_dir`、`output` 可由规格或命令行给出；命令行优先。
- 约束 `id` 必须唯一；缺省为 `constraint_<序号>`。

约束类型及参数：

| 类型 | 参数 | 语义 |
|---|---|---|
| `min_rooms` | `count >= 0` | 房间数至少为 `count`，且不超过可用格数 |
| `max_area` | `max_area >= 1` | 每个房间面积不超过上限 |
| `room_spacing` | `min_spacing >= 0` | 任意两矩形房间之间至少留出该数量的空格 |
| `connectivity` | 无 | 所有房间/走廊格属于同一个四连通区域 |
| `start_end_reachable` | `start`, `end` | 起点到终点不经过不可用格且彼此可达 |

网格输出编码：`-1` 不可用格、`0` 空白、`1` 房间、`2` 走廊。结果同时附带 ASCII 视图（`#`、`.`、`R`、`+`）。

## 求解与回退

1. 按约束声明顺序逐个应用；每个约束由当前状态和局部确定性 RNG 生成候选。
2. 候选先做完整性检查，再用有界 `ThreadPoolExecutor` 并行检查此前所有约束及当前约束。
3. 预检按约束声明顺序收集结果，因此首个错误与串行预检一致；失败时取消尚未开始的其余任务。
4. 当前约束所有候选失败时，DFS 回退到最近的前序约束并尝试其下一候选；冲突约束对会记录在结果中。
5. 一次回退跨过的约束层数不得超过 `rollback_limit`。超过上限返回 `E_CONFLICT`，报告中包含无法满足的约束 id，不写最终输出文件。

`attempts_total`、`rollback_count` 和已选候选序号都会写入快照，续算后仍与不间断求解保持一致。

引擎一次只保留一个候选网格状态；线程池大小取 `concurrency`、待检约束数和“内存预算可容纳状态数”三者的最小值。结果中的 `preflight` 统计会报告状态内存估算、预算可容纳状态数和实际并发 worker 数。

## 快照与版本

```text
snapshots/
  manifest.json
  interrupt.json                  # 仅处理中断时存在
  snapshots/
    step-000001-<version-prefix>.json
    ...
  archive/
    runs/                         # 规格/种子变化或从头重做时的旧运行
    superseded/                   # 回退后不再位于活动链的旧分支
    corrupt/                      # 校验失败、已被安全隔离的快照
```

每份快照包含网格、房间矩形、已应用约束序号、已选候选序号、种子、规范化规格摘要、回退计数、尝试计数、父版本标识和内容摘要。

版本标识由规范化内容计算：

```text
sha256({
  grid, rooms, applied_count, applied_constraints, selected_attempts,
  seed, spec_digest, parent_version_id, commit_sequence
})
```

快照内容摘要覆盖除 `digest` 外的完整 JSON。`commit_sequence` 表示持久化提交顺序，使强制终止后能区分“已写快照但清单尚未更新”的最新状态。规格摘要包含约束语义与顺序、种子、网格、不可用格、回退/尝试上限；不含 `concurrency`、内存预算、快照路径和输出路径，因此调整运行时并发不会改变求解结果或版本。

快照文件名含版本前缀，采用临时文件 + `fsync` + 原子替换。已有文件不会被静默覆盖：版本变化时，旧运行进入 `archive/runs/`，旧分支进入 `archive/superseded/`，校验失败文件进入 `archive/corrupt/`。

## 中断、续算与拒绝策略

- 捕获 `SIGINT`/`SIGTERM` 后仅在约束边界停止，原子写入 `interrupt.json`，退出码 `75`。
- 续算优先读取中断标记和最新有效快照。标记、清单与快照版本一致时从下一约束继续。
- 无标记（例如强制终止）时使用清单指向的最新有效快照；若清单不可用，则扫描并选择应用序号最高的有效快照。
- 标记版本过期或与清单不一致时归档旧运行并从头重做。
- 快照摘要缺失/不符、版本重算不符、状态与已应用约束不符时判定失效。
- 默认续算会拒绝失效快照并回退至最近有效快照；若无任何有效快照则从头重做，结果中包含 `E_DIGEST` 警告。`verify` 命令与 `--strict-digest` 模式直接返回 `E_DIGEST`。

## 错误码与退出码

| 退出码 | 错误码 | 含义 |
|---|---|---|
| `0` | - | 成功 |
| `2` | `E_JSON` / `E_SPEC` / `E_WIDTH` / `E_HEIGHT` / `E_BLOCKED` / `E_SEED` / `E_CONSTRAINT_TYPE` / `E_CONSTRAINT_PARAM` / `E_CONFIG` / `E_RESOURCE` | 输入、配置或资源域错误 |
| `3` | `E_CONFLICT` | 超过回退上限仍无法满足约束 |
| `4` | `E_DIGEST` | 快照/清单/标记摘要或版本校验失败 |
| `70` | `E_IO` / `E_INTERNAL` | 本地 I/O 或内部错误 |
| `75` | `E_INTERRUPTED` | 已在约束边界中断并持久化标记 |

非法规格在创建快照目录或输出文件前即被拒绝；错误以 JSON 写到标准输出。

## 测试与验收

```bash
python -m unittest discover -s tests -v
python scripts/acceptance.py
```

本次实际结果：16 项测试全部通过；验收报告见 `docs/verification_report.json`，技术记录见 `docs/VERIFICATION.md`。
